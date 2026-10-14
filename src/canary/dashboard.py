"""Runs dashboard daemon: serve the runs table UI and own run processes.

Localhost-only control plane (default 127.0.0.1:8789). Owns container runs
it starts (spawn/wait/register), pauses and unpauses live worker
containers, relaunches recorded specs, and serves the dashboard UI plus a
JSON API. No auth: never bound beyond loopback, owner machine only.

Runs survive daemon exit by design (detached process group); on every
listing the daemon reconciles registry rows against live containers, so a
restart never leaves phantom "running" rows.
"""
from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote, unquote
from xml.etree import ElementTree

from . import runs

QUESTION_ENGINE_ROOT = Path(os.environ.get("CANARY_QUESTION_ENGINE_ROOT", runs.ROOT)).resolve()
DEFAULT_PORT = 8789
MAX_BODY = 64 * 1024
FINDINGS_TTL_S = 60
INSPECT_TTL_S = 2

UI_PATH = runs.ROOT / "scripts" / "dashboard" / "index.html"
INTER_FONT_PATH = UI_PATH.parent / "assets" / "InterVariable.woff2"
RESEARCH_CSS_PATH = UI_PATH.parent / "assets" / "research.css"
FINLANDICA_FONT_PATH = UI_PATH.parent / "assets" / "Finlandica-Regular.woff2"
FINLANDICA_LICENSE_PATH = UI_PATH.parent / "assets" / "Finlandica-LICENSE.txt"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "run")[:40]


def validate_launch(argv: list) -> list[str]:
    """Only the disposable-worker launcher may be started (contained by design).

    Anything else is refused: the daemon never runs arbitrary host commands.
    Tests monkeypatch this to substitute a stub launcher.
    """
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty string list")
    coverage_launcher = (QUESTION_ENGINE_ROOT / "scripts/coverage_run.sh").resolve()
    if len(argv) >= 2 and Path(argv[1]).resolve() == coverage_launcher:
        if len(argv) != 4 or argv[0] != "bash" or argv[2] != "ask" or not argv[3].strip():
            raise ValueError("coverage launch must be bash scripts/coverage_run.sh ask QUESTION")
        return list(argv)
    if len(argv) < 2 or argv[0] not in ("bash", "sh"):
        raise ValueError("launch must invoke an approved research launcher via bash/sh")
    if not argv[1].replace("\\", "/").endswith("scripts/container_run.sh"):
        raise ValueError("launch must invoke scripts/container_run.sh")
    return list(argv)


def _docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


class Supervisor:
    """Owns daemon-started runs; reconciles registry against live containers."""

    def __init__(self, registry: str | Path = runs.REGISTRY,
                 root: str | Path = runs.ROOT):
        self.registry = Path(registry)
        self.root = Path(root)
        self.owned: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self._inspect_cache: dict[str, tuple[float, dict]] = {}
        self.findings_cache: dict = {"at": 0.0, "data": None}

    # -- spawning --

    def start(self, name: str, brief: str, argv: list[str],
              env: dict | None = None) -> dict:
        argv = validate_launch(argv)
        extra = runs.sanitize_launch_env(env)
        if not name.strip() or len(name) > 120:
            raise ValueError("name must be 1..120 chars")
        if len(brief) > 2000:
            raise ValueError("brief must be at most 2000 chars")
        stamp = runs.utcnow()[:16].replace(":", "").replace("T", "-").replace("-", "")
        base = f"{slugify(name)}-{stamp}"
        key, bundle, n = base, None, 0
        while True:
            candidate = base if n == 0 else f"{base}-{n}"
            out = self.root / "container-out" / candidate
            if not out.exists() and runs.get(candidate, self.registry) is None:
                key, bundle = candidate, out
                break
            n += 1
        native = (len(argv) > 2 and argv[2] == "ask" and
                  Path(argv[1]).resolve() in {(QUESTION_ENGINE_ROOT / "scripts/container_run.sh").resolve(),
                                              (QUESTION_ENGINE_ROOT / "scripts/coverage_run.sh").resolve()})
        kind = "question" if native else argv[2] if len(argv) > 2 else "?"
        launch_root = QUESTION_ENGINE_ROOT if native else self.root
        env = dict(os.environ, OUT=str(bundle), RUN_KEY=key,
                   RUN_NAME=name, RUN_BRIEF=brief,
                   CANARY_RUNS_REGISTRY=str(self.registry))
        env.update(extra)
        launcher_log = self.root / "container-out" / f"{key}.launcher.log"
        launcher_log.parent.mkdir(parents=True, exist_ok=True)
        logfh = open(launcher_log, "a", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(argv, cwd=launch_root, env=env,
                                    stdout=logfh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        except Exception:
            logfh.close()
            raise
        with self.lock:
            self.owned[key] = proc
        runs.register_start(key, name, brief, kind, str(bundle), "",
                            shlex.join(argv), path=self.registry,
                            launch_argv=list(argv), launch_env=extra)
        threading.Thread(target=self._wait, args=(key, proc, logfh),
                         daemon=True).start()
        return {"key": key, "bundle": str(bundle)}

    def start_question(self, question: str, name: str = "", brief: str = "") -> dict:
        question = question.strip()
        if not question:
            raise ValueError("question cannot be empty")
        return self.start(name or question[:120], brief or question[:2000],
                          ["bash", str(QUESTION_ENGINE_ROOT / "scripts/coverage_run.sh"), "ask", question])

    def _wait(self, key: str, proc: subprocess.Popen, logfh) -> None:
        try:
            rc = proc.wait()
        finally:
            try:
                logfh.close()
            except OSError:
                pass
        # Finish and drop ownership atomically: reconcile interrupts any
        # unowned running row, so the two must never be observed apart.
        row = runs.get(key, self.registry)
        bundle = row.get("bundle", "") if row else ""
        with self.lock:
            try:
                runs.register_finish(key, "done" if rc == 0 else "failed",
                                     bundle, path=self.registry)
            finally:
                self.owned.pop(key, None)

    # -- live state --

    def inspect(self, container: str) -> dict:
        """Cached docker state probe. Missing docker/daemon → {live: False}."""
        now = time.monotonic()
        hit = self._inspect_cache.get(container)
        if hit and now - hit[0] < INSPECT_TTL_S:
            return hit[1]
        state: dict = {"live": False, "container_state": "missing"}
        try:
            r = _docker("inspect", "-f", "{{.State.Running}} {{.State.Paused}}",
                        container, timeout=10)
            if r.returncode == 0:
                running, paused = r.stdout.strip().split()
                live = running == "true"
                state = {"live": live,
                         "container_state": ("paused" if paused == "true"
                                             else "running" if live else "exited")}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        self._inspect_cache[container] = (now, state)
        return state

    def reconcile(self) -> list[dict]:
        """Fold live truth over registry rows; finish stale launch rows."""
        rows = runs.load(self.registry)
        for row in rows:
            container = row.get("container") or ""
            if not container and row.get("bundle"):
                bundle = Path(row["bundle"])
                if not bundle.is_absolute():
                    bundle = self.root / bundle
                native = _load_json_file(bundle / "run.json", {})
                if isinstance(native, dict) and native.get("kind") == "question" and (
                        native.get("execution_mode") in ("saved full-paper Codex CLI research", "coverage-guided full-paper research")
                        or type(native.get("cli_invocations")) is int and native["cli_invocations"] > 0
                        or isinstance(native.get("cli_thread_id"), str) and native["cli_thread_id"].strip()):
                    # External CLI runs publish their own state and timing; a
                    # missing Docker container does not mean they have stopped.
                    parsed = runs.parse_bundle(bundle)
                    if row.get("status") in runs._FAILURE_STATUS and parsed.get("status") in runs._ACTIVE_STATUS:
                        parsed.update(status=row["status"], ended_at=row.get("ended_at"))
                    row.update(parsed)
                    row["live"] = False
                    continue
            if row.get("status") in ("starting", "running", "paused") and container:
                row["live"] = self.inspect(container)["live"]
                row["container_state"] = self.inspect(container)["container_state"]
            else:
                row["live"] = False
            with self.lock:
                owned = row.get("key") in self.owned
                stale = row.get("status") in ("starting", "running", "paused")
                fresh = (runs.get(row["key"], self.registry)
                         if stale and not row["live"] and not owned else None)
                if (fresh is not None
                        and fresh.get("status") in ("starting", "running", "paused")):
                    bundle = row.get("bundle", "")
                    # Guest-exited-but-exported rows carry bundle truth
                    # (run.json is sealed at cycle end); only genuinely
                    # bundle-less rows are interruptions. The launcher's
                    # own finish lands seconds later and no-ops either way.
                    status = ("done"
                              if (Path(str(bundle)) / "run.json").exists()
                              else "interrupted")
                    finished = runs.register_finish(
                        row["key"], status, bundle, path=self.registry)
                else:
                    finished = None
                if finished:
                    row.update(finished)
                    row["live"] = False
        return rows

    def pause(self, key: str) -> dict:
        row = runs.get(key, self.registry)
        if row is None:
            raise KeyError(key)
        container = row.get("container") or ""
        if not container:
            raise RuntimeError("run has no container yet (launcher not started)")
        state = self.inspect(container)
        if not state["live"]:
            raise RuntimeError("container is not live")
        if state["container_state"] == "paused":
            return {"key": key, "status": "paused"}
        r = _docker("pause", container)
        if r.returncode != 0:
            raise RuntimeError(f"docker pause failed: {r.stderr.strip()[:200]}")
        self._inspect_cache.pop(container, None)
        row["status"] = "paused"
        runs.upsert(row, self.registry)
        return {"key": key, "status": "paused"}

    def unpause(self, key: str) -> dict:
        row = runs.get(key, self.registry)
        if row is None:
            raise KeyError(key)
        container = row.get("container") or ""
        if not container:
            raise RuntimeError("run has no container yet (launcher not started)")
        state = self.inspect(container)
        if state["container_state"] != "paused":
            if state["live"]:
                row["status"] = "running"
                runs.upsert(row, self.registry)
                return {"key": key, "status": "running"}
            raise RuntimeError("container is not live")
        r = _docker("unpause", container)
        if r.returncode != 0:
            raise RuntimeError(f"docker unpause failed: {r.stderr.strip()[:200]}")
        self._inspect_cache.pop(container, None)
        row["status"] = "running"
        runs.upsert(row, self.registry)
        return {"key": key, "status": "running"}

    def rerun(self, key: str, name: str = "", brief: str = "") -> dict:
        row = runs.get(key, self.registry)
        if row is None:
            raise KeyError(key)
        argv = row.get("launch_argv") or []
        if not (isinstance(argv, list) and argv
                and all(isinstance(a, str) for a in argv)):
            launch = row.get("launch", "")
            if not launch:
                raise RuntimeError("run has no recorded launch spec")
            try:
                argv = shlex.split(launch)
            except ValueError as e:
                raise RuntimeError(
                    f"recorded launch is not parseable: {e}") from e
        new_name = name or row.get("name", key) + " (rerun)"
        new_brief = brief or (row.get("brief", "") + f"\nRerun of {key}.")
        started = self.start(new_name, new_brief, argv,
                             env=row.get("launch_env") or {})
        started["from"] = key
        return started


RESEARCH_PAPERS_CAP = 200
RESEARCH_ABSTRACT_CHARS = 1500
RESEARCH_MD_CHARS = 12000
RESEARCH_REVIEW_CHARS = 6000
RESEARCH_DOC_CHARS = 6000
RESEARCH_DETAIL_CHARS = 300
SUMMARY_READ_CHARS = 4 * 1024 * 1024
SUMMARY_FILE_CHARS = 1024 * 1024
SUMMARY_DIRECTORY_ENTRIES_CAP = 300
SUMMARY_EXCERPT_CHARS = 420


def _read_capped(path: Path, limit: int) -> tuple[str | None, bool]:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            text = stream.read(limit + 1)
    except (OSError, ValueError):
        return None, False
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _load_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _launch_question(row: dict) -> str:
    argv = row.get("launch_argv")
    if not (isinstance(argv, list) and all(isinstance(a, str) for a in argv)
            and argv):
        launch = row.get("launch")
        try:
            argv = shlex.split(launch) if isinstance(launch, str) else []
        except ValueError:
            return ""
    for i, arg in enumerate(argv[:-2]):
        if (arg.replace("\\", "/").endswith("scripts/container_run.sh")
                and argv[i + 1] in ("ask", "cycle", "review", "analyze")):
            question = argv[i + 2].strip()
            return question if not question.startswith("--") else ""
    return ""


def _synthesis_excerpt(markdown: str) -> str:
    # The cycle report wraps the answer in execution metadata and a trail.
    text = re.split(r"(?m)^## (?:Trail|Unanswered)\b", markdown, maxsplit=1)[0]
    text = re.sub(r"(?m)^# Unattended run:.*$", "", text)
    text = re.sub(r"(?m)^\*\*Final Report:.*\*\*\s*$", "", text)
    text = re.sub(r"(?m)^_(?:Iterations:|model calls:).*_$", "", text)
    text = text.replace(
        "_No final synthesis: see stopped reason and unanswered questions._", "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+\.\s+)", "", text)
    text = re.sub(r"[*_`~]", "", text)
    text = " ".join(html.unescape(text).split())
    if len(text) > SUMMARY_EXCERPT_CHARS:
        return text[:SUMMARY_EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def research_summary(row: dict) -> dict:
    """Small host-bundle summary; never snapshot workers or mutate registry.

    Reads share a four-million-character budget. A truncated summary exposes
    lower-bound counts explicitly, while missing/partial files stay usable.
    """
    summary = {"question": "", "synthesis_excerpt": "", "has_synthesis": False,
               "papers_count": 0, "claims_count": 0, "supported_claims_count": 0,
               "iterations_count": 0, "unanswered_count": 0, "truncated": False}
    bundle = row.get("bundle")
    native_path = Path(bundle) if isinstance(bundle, str) and bundle.strip() else None
    if native_path is not None:
        native_run = _load_json_file(native_path / "run.json", {})
        if isinstance(native_run, dict) and native_run.get("kind") == "question":
            graph = _load_json_file(native_path / "answer.json", {})
            graph = graph if isinstance(graph, dict) else {}
            claims = [c for c in graph.get("claims", []) if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip()]
            access = _load_json_file(native_path / "access.json", {})
            access = access if isinstance(access, dict) else {}
            prose = " ".join(text for p in graph.get("executive_summary", [])
                             for text in [p.get("text") if isinstance(p, dict) else p] if isinstance(text, str)).strip()
            summary.update(question=native_run.get("question", graph.get("question", "")),
                           synthesis_excerpt=_synthesis_excerpt(prose), has_synthesis=bool(prose),
                           papers_count=native_run.get("discovered", len(access.get("accepted", []))),
                           claims_count=len(claims), supported_claims_count=sum(bool(c.get("evidence_ids")) for c in claims if isinstance(c, dict)),
                           unanswered_count=len(graph.get("unanswered", [])))
            return summary
    remaining = SUMMARY_READ_CHARS
    bundle = row.get("bundle")
    b = Path(bundle) if isinstance(bundle, str) and bundle.strip() else None

    def read(name: str, limit: int = SUMMARY_FILE_CHARS) -> str:
        nonlocal remaining
        if b is None:
            return ""
        value, truncated = _read_capped(b / name, min(limit, remaining))
        remaining -= len(value or "")
        summary["truncated"] |= truncated
        return value or ""

    def load(name: str) -> dict:
        try:
            value = json.loads(read(name))
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    run = load("run.json")
    manifest = load("manifest.json")
    spec = manifest.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    for question in (run.get("seed"), spec.get("question"), row.get("seed"),
                     _launch_question(row)):
        if isinstance(question, str) and question.strip() not in ("", "?"):
            summary["question"] = question.strip()
            break
    summary["synthesis_excerpt"] = _synthesis_excerpt(read("synthesis.md", 16000))
    summary["has_synthesis"] = bool(summary["synthesis_excerpt"])
    unanswered = run.get("unanswered")
    summary["unanswered_count"] = len(unanswered) if isinstance(unanswered, list) else 0

    iterations = {}
    embedded = run.get("iterations")
    for i, iteration in enumerate(embedded if isinstance(embedded, list) else []):
        if isinstance(iteration, dict):
            iterations[str(iteration.get("n", i + 1))] = iteration
    if b is not None:
        try:
            with os.scandir(b / "iterations") as entries:
                for index, entry in enumerate(entries):
                    if index >= SUMMARY_DIRECTORY_ENTRIES_CAP:
                        summary["truncated"] = True
                        break
                    if re.fullmatch(r"iter\d+\.json", entry.name):
                        iteration = load("iterations/" + entry.name)
                        if iteration:
                            iterations[str(iteration.get("n", entry.name[4:-5]))] = iteration
        except OSError:
            pass
    summary["iterations_count"] = len(iterations)
    for iteration in iterations.values():
        claims = iteration.get("claims")
        for claim in claims if isinstance(claims, list) else []:
            if isinstance(claim, dict):
                summary["claims_count"] += 1
                summary["supported_claims_count"] += claim.get("support") == "supported"

    papers = set()
    for line in read("retrieval.jsonl", remaining).splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        candidates = entry.get("papers") if isinstance(entry, dict) else None
        for paper in candidates if isinstance(candidates, list) else []:
            if isinstance(paper, dict):
                identity = paper.get("ref") or paper.get("doi") or paper.get("title")
                if isinstance(identity, str) and identity.strip():
                    papers.add(identity.strip())
    summary["papers_count"] = len(papers)
    return summary


def _journal_detail(ev: dict) -> str:
    return str(ev.get("detail", ev.get("Detail", "")))


def live_snapshot(container: str, dest: Path, timeout: int = 20) -> bool:
    """Copy a live worker's /work/out into dest. Host-side read, best-effort.

    Same trust level as pause/unpause: the daemon already controls the
    container's lifecycle. Any failure (exited mid-copy, docker hiccup)
    returns False and the caller falls back to the host bundle.
    """
    try:
        r = _docker("cp", f"{container}:/work/out/.", str(dest),
                    timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and (dest / "run.json").exists()


def research_for_row(row: dict, supervisor: "Supervisor") -> tuple[dict, bool]:
    """Research record for a registry row, live-snapping running workers."""
    bundle = row.get("bundle", "")
    if (Path(str(bundle)) / "run.json").exists():
        return research_bundle(bundle), False
    container = row.get("container") or ""
    if (row.get("status") in ("running", "paused") and container
            and supervisor.inspect(container)["live"]):
        tmpdir = Path(tempfile.mkdtemp(prefix="canary-research-"))
        try:
            if live_snapshot(container, tmpdir):
                return research_bundle(tmpdir), True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return research_bundle(bundle), False


def research_bundle(bundle: str | Path) -> dict:
    """Full research record for one run: papers, claims, synthesis, decisions.

    Every read is guarded: a running or partial bundle yields present=False /
    empty lists, never a 500. Large blobs are capped with truncated flags.
    """
    b = Path(bundle)
    run = _load_json_file(b / "run.json", {})
    if not isinstance(run, dict):
        run = {}

    if run.get("kind") == "question":
        return question_research_bundle(b, run)

    synthesis_md, synthesis_truncated = _read_capped(
        b / "synthesis.md", RESEARCH_MD_CHARS)

    papers: list[dict] = []
    papers_total = 0
    seen_refs: set[str] = set()
    try:
        retrieval_lines = (b / "retrieval.jsonl").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        retrieval_lines = []
    for line in retrieval_lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        candidates = entry.get("papers", []) if isinstance(entry, dict) else []
        if not isinstance(candidates, list):
            continue
        for p in candidates:
            if not isinstance(p, dict):
                continue
            papers_total += 1
            ref = str(p.get("ref") or p.get("title") or "")
            if not ref or ref in seen_refs:
                continue
            seen_refs.add(ref)
            if len(papers) >= RESEARCH_PAPERS_CAP:
                continue
            abstract = str(p.get("abstract", ""))
            papers.append({
                "ref": ref,
                "title": str(p.get("title", "")),
                "year": p.get("year", ""),
                "source": str(p.get("source", "")),
                "doi": str(p.get("doi", "")),
                "oa_url": str(p.get("oa_url", "")),
                "citations": p.get("citations", 0),
                "abstract": abstract[:RESEARCH_ABSTRACT_CHARS],
                "abstract_truncated": len(abstract) > RESEARCH_ABSTRACT_CHARS,
            })

    # Finished bundles can retain iterations only in run.json. Incremental
    # sidecars take precedence when both representations describe the same n.
    iteration_records: dict[str, tuple[dict, Path]] = {}
    embedded = run.get("iterations")
    for index, d in enumerate(embedded if isinstance(embedded, list) else []):
        if not isinstance(d, dict):
            continue
        number = str(d.get("n", index + 1))
        # Embedded content must never become an arbitrary review-file path.
        stem_number = number if re.fullmatch(r"[0-9]+", number) else str(index + 1)
        iteration_records[number] = (d, b / "iterations" / f"iter{stem_number}.json")
    for path in sorted(b.glob("iterations/iter*.json")):
        d = _load_json_file(path, {})
        if isinstance(d, dict) and d:
            iteration_records[str(d.get("n", path.stem[4:]))] = (d, path)

    def iteration_order(item):
        try:
            return (0, int(item[0]))
        except ValueError:
            return (1, item[0])

    iterations = []
    for _, (d, path) in sorted(iteration_records.items(), key=iteration_order):
        claims = []
        for c in d.get("claims", []) if isinstance(d.get("claims"), list) else []:
            if not isinstance(c, dict):
                continue
            ev_list = []
            for e in (c.get("evidence", [])
                      if isinstance(c.get("evidence"), list) else []):
                if not isinstance(e, dict):
                    continue
                ev_list.append({
                    "paper": e.get("paper", ""),
                    "span": str(e.get("span", ""))[:400],
                    "anchored": bool(e.get("anchored", False)),
                })
            claims.append({
                "id": str(c.get("id", "")),
                "text": str(c.get("text", ""))[:1000],
                "support": str(c.get("support", "")),
                "scope": str(c.get("scope", "")),
                "uncertainty": str(c.get("uncertainty", ""))[:300],
                "evidence": ev_list,
            })
        validation = d.get("validation", {})
        if not isinstance(validation, dict):
            validation = {}
        review_md, review_truncated = _read_capped(
            path.with_name(path.stem + "-review.md"), RESEARCH_REVIEW_CHARS)
        titles = d.get("papers", [])
        iterations.append({
            "n": d.get("n", ""),
            "kind": str(d.get("kind", "")),
            "question": str(d.get("question", "")),
            "papers_count": len(titles) if isinstance(titles, list) else 0,
            "papers": [str(t) for t in titles][:100] if isinstance(titles, list) else [],
            "claims": claims,
            "validation": {
                "claims_parsed": validation.get("claims_parsed", 0),
                "batches": validation.get("batches", 0),
                "dangling_citations": validation.get("dangling_citations", []),
                "unresolved": [str(u)[:200] for u in
                               validation.get("unresolved", [])][:10]
                if isinstance(validation.get("unresolved"), list) else [],
            },
            "review_md": review_md,
            "review_truncated": review_truncated,
        })

    assessments = []
    for path in sorted(b.glob("assessments/*.json")):
        d = _load_json_file(path, {})
        if not isinstance(d, dict):
            continue
        doc = str(d.get("doc_markdown", ""))
        if not doc:
            sibling = path.with_suffix(".md")
            doc, _ = _read_capped(sibling, RESEARCH_DOC_CHARS)
            doc = doc or ""
        assessments.append({
            "id": str(d.get("id", path.stem)),
            "ts": str(d.get("ts", "")),
            "outcome": str(d.get("outcome", "")),
            "calls_spent": d.get("calls_spent", 0),
            "doc_markdown": doc[:RESEARCH_DOC_CHARS],
            "doc_truncated": len(doc) > RESEARCH_DOC_CHARS,
        })

    decisions = [{"t": ev.get("ts", ""), "seq": ev.get("seq", 0),
                  "phase": str(ev.get("phase", "")),
                  "event": str(ev.get("event", "")),
                  "detail": _journal_detail(ev)[:RESEARCH_DETAIL_CHARS]}
                 for ev in runs.journal_events(b)]

    usage = run.get("usage", {})
    return {
        "seed": run.get("seed", "?"),
        "stopped": run.get("stopped", "?"),
        "model": run.get("model", "?"),
        "usage": usage if isinstance(usage, dict) else {},
        "unanswered": [str(u) for u in run.get("unanswered", [])]
        if isinstance(run.get("unanswered"), list) else [],
        "synthesis_md": synthesis_md,
        "synthesis_truncated": synthesis_truncated,
        "papers": papers,
        "papers_total": papers_total,
        "papers_truncated": len(seen_refs) > len(papers),
        "iterations": iterations,
        "assessments": assessments,
        "decisions": decisions,
    }


def question_research_bundle(bundle: Path, run: dict) -> dict:
    """Native question artifacts are returned intact, without legacy UI caps."""
    def load(name, default):
        value = _load_json_file(bundle / name, default)
        return value if isinstance(value, type(default)) else default
    graph, access = load("answer.json", {}), load("access.json", {})
    review = {key: value for key, value in load("peer-review.json", {}).items() if key != "revised_answer"}
    papers = load("abstracts.json", [])
    if not papers:
        papers = [ref.get("paper", {}) for ref in graph.get("references", []) if isinstance(ref, dict)]
    try:
        markdown = (bundle / "answer.md").read_text(encoding="utf-8")
    except OSError:
        markdown = None
    parsed = runs.parse_bundle(bundle)
    return {"kind": "question", "answer": graph, "run": run, "peer_review": review,
            "query_net": load("query-net.json", {}), "access": access,
            "source_recovery": load("source-recovery.json", {}),
            "coverage": load("coverage.json", {}), "paper_selection": load("paper-selection.json", {}),
            "profile": load("profile.json", {}), "source_efficiency": load("source-efficiency.json", {}),
            "seed": run.get("question", graph.get("question", "?")),
            "stopped": run.get("status", "?"), "model": run.get("model", graph.get("model", "?")),
            "usage": parsed.get("usage", {}), "unanswered": graph.get("unanswered", []),
            "synthesis_md": markdown, "synthesis_truncated": False,
            "papers": papers, "papers_total": run.get("discovered", len(papers)), "papers_truncated": False,
            "iterations": [], "assessments": [], "decisions": runs.journal_events(bundle)}


def _saved_source_path(bundle: Path, value) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    for prefix in ("/export/", "/work/out/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "papers":
        return None
    candidate = bundle / relative
    try:
        if candidate.is_file() and candidate.resolve().is_relative_to(bundle.resolve()):
            return candidate
    except OSError:
        pass
    return None


def source_artifacts(bundle: str | Path, source_id: str) -> tuple[dict, Path | None, Path | None, dict]:
    if not str(bundle).strip():
        raise KeyError(source_id)
    b = Path(bundle)
    graph = _load_json_file(b / "answer.json", {})
    graph = graph if isinstance(graph, dict) else {}
    reference = next((r for r in graph.get("references", []) if isinstance(r, dict) and r.get("id") == source_id), None)
    if reference is None:
        raise KeyError(source_id)
    paper = reference.get("paper", {})
    paper = paper if isinstance(paper, dict) else {}
    extra = paper.get("extra", {})
    extra = extra if isinstance(extra, dict) else {}
    access = _load_json_file(b / "access.json", {})
    access = access if isinstance(access, dict) else {}
    document = next((d for d in access.get("accepted", []) if isinstance(d, dict)
                     and isinstance(d.get("paper"), dict) and d["paper"].get("ref") == paper.get("ref")), {})
    original = _saved_source_path(b, extra.get("local_file")) or _saved_source_path(b, document.get("file_path"))
    text = _saved_source_path(b, document.get("text_path"))
    if text is None and original is not None:
        text = _saved_source_path(b, str(original.relative_to(b).with_suffix(".txt")))
    return reference, original, text, graph


def _source_headings(text: str, original: Path | None):
    if original is not None and original.suffix.lower() == ".xml":
        try:
            root = ElementTree.parse(original).getroot()
            titles = set()
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1].lower() in ("sec", "div"):
                    for child in element:
                        if child.tag.rsplit("}", 1)[-1].lower() in ("title", "head"):
                            title = " ".join("".join(child.itertext()).split())
                            if title:
                                titles.add(title)
            headings = [match for title in titles for match in re.finditer(
                r"(?m)^[ \t]*" + re.escape(title) + r"[ \t]*$", text)]
            if headings:
                return sorted(headings, key=lambda match: match.start()), True
        except (OSError, ElementTree.ParseError):
            pass
    return list(re.finditer(r"(?im)^[ \t]*(?:[0-9]+[.)]?[ \t]+)?(?:abstract|introduction|background|methods?|materials(?: and methods)?|results|discussion|conclusions?|references)[ \t]*$", text)), False


def source_context(bundle: str | Path, source_id: str, paragraph_id: str = "", scope: str = "section", key: str = "") -> dict:
    if scope not in ("section", "paper"):
        raise ValueError("scope must be section or paper")
    reference, original, text_path, graph = source_artifacts(bundle, source_id)
    payload = {"source_id": source_id, "reference": reference, "paragraphs": [], "page": None,
               "local_file_url": f"/api/runs/{quote(key, safe='')}/sources/{quote(source_id, safe='')}/file" if original else None,
               "original_url": reference.get("text_url") or reference.get("paper", {}).get("url"),
               "requested_scope": scope, "scope": "paper", "context_label": "Full readable paper",
               "text_available": False, "text_complete": None, "offset_verified": False, "warning": ""}
    selected = next((p for p in graph.get("passages", []) if isinstance(p, dict) and p.get("id") == paragraph_id
                     and p.get("source_id") == source_id), None)
    abstract_selected = bool(selected and selected.get("kind") == "abstract")
    kind = "abstract" if abstract_selected and scope == "section" else "fulltext"
    if kind == "abstract":
        text = reference.get("paper", {}).get("abstract", "")
        text = text if isinstance(text, str) else ""
        payload.update(scope="abstract", context_label="Abstract", text_complete=bool(text))
    else:
        if text_path is None:
            payload["warning"] = "Saved readable full-source text is missing; abstracts are not substituted."
            return payload
        with text_path.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
        expected_chars = reference.get("text_chars") if reference.get("kind") != "abstract" else None
        if type(expected_chars) is int:
            payload["text_complete"] = len(text) == expected_chars
            if not payload["text_complete"]:
                payload["warning"] = "Saved source text length differs from the recorded full paper; context may be incomplete."
        if abstract_selected:
            selected = None
            payload["warning"] += (" " if payload["warning"] else "") + "Selected evidence is from the abstract; full-paper body context has no abstract highlighting."
    payload["text_available"] = bool(text.strip())
    if paragraph_id and selected is None and not abstract_selected:
        raise ValueError("paragraph does not belong to this source")
    selection = None
    if selected:
        a, z = selected.get("start"), selected.get("end")
        if type(a) is int and type(z) is int and 0 <= a < z <= len(text) and text[a:z] == selected.get("text"):
            selection = (a, z)
            payload["offset_verified"] = True
        else:
            payload["warning"] = "The " + ("reference abstract" if kind == "abstract" else "saved source text") + " does not match the recorded passage offsets; highlighting is unavailable."
    markers = list(re.finditer(r"\[PDF page ([0-9]+)\]", text)) if kind == "fulltext" else []
    def page_at(offset):
        return next((int(m[1]) for m in reversed(markers) if m.start() <= offset), None)
    begin, end = 0, len(text)
    if selection:
        payload["page"] = page_at(selection[0])
        if scope == "section" and markers:
            marker = next((m for m in reversed(markers) if m.start() <= selection[0]), None)
            if marker:
                begin = marker.start()
                end = next((m.start() for m in markers if m.start() > begin), len(text))
                payload.update(scope="page", context_label=f"PDF page {payload['page']}")
        elif scope == "section" and kind == "fulltext":
            headings, structural = _source_headings(text, original)
            heading = next((m for m in reversed(headings) if m.start() <= selection[0]), None)
            if heading:
                begin = heading.start()
                end = next((m.start() for m in headings if m.start() > begin), len(text))
                payload.update(scope="section" if structural else "context",
                               context_label=heading[0].strip() if structural else "From " + heading[0].strip() + " onward")
            else:
                payload["context_label"] = "Full readable paper (section heading unavailable)"
    for match in re.finditer(r"\S[\s\S]*?(?=\n[ \t]*\n|\Z)", text[begin:end]):
        a, z = begin + match.start(), begin + match.end()
        while z > a and text[z - 1].isspace():
            z -= 1
        cuts = sorted({a, z} | ({point for point in selection if a < point < z} if selection else set()))
        for start, stop in zip(cuts, cuts[1:]):
            if not text[start:stop].strip():
                continue
            payload["paragraphs"].append({"id": f"{source_id}-{kind}-{start}-{stop}", "kind": kind, "text": text[start:stop],
                                           "start": start, "end": stop, "page": page_at(start),
                                           "selected": bool(selection and selection[0] <= start < stop <= selection[1])})
    return payload


def _coverage_search_status() -> dict | None:
    path = QUESTION_ENGINE_ROOT / "out" / "coverage-openalex-usage.sqlite"
    if not path.is_file():
        return None
    date = runs.utcnow()[:10]
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            count = db.execute("SELECT COUNT(*) FROM searches WHERE at LIKE ?", (date + "%",)).fetchone()[0]
            first = db.execute("SELECT MIN(at) FROM searches").fetchone()[0]
        return {"available": True, "date_utc": date, "keyword_searches_today": count,
                "logging_started_at_utc": first, "full_day_observed": False,
                "count_scope": "Successful coverage operator search requests, each cursor page counted; gateway and other clients excluded.",
                "quota_status": "Operator count only; remaining provider quota unavailable."}
    except (OSError, sqlite3.Error):
        return None


def openalex_status() -> dict:
    """Use the gateway's narrow token only inside its runtime, never in the browser."""
    native = _coverage_search_status()
    if native is not None:
        return native
    script = ("import json,urllib.request;from pathlib import Path;"
              "token=Path('/state/access.key').read_text().strip();"
              "request=urllib.request.Request('http://127.0.0.1:8787/v1/sources/openalex/status',"
              "headers={'Authorization':'Bearer '+token});"
              "print(json.dumps(json.load(urllib.request.urlopen(request,timeout=10))))")
    try:
        response = _docker("exec", "canary-gate", "python", "-c", script, timeout=15)
        if response.returncode != 0:
            return {"available": False, "warning": "OpenAlex source status is unavailable."}
        data = json.loads(response.stdout)
        if not isinstance(data, dict):
            raise ValueError("Invalid status")
        safe = {k: data[k] for k in ("available", "date_utc", "keyword_searches_today", "logging_started_at_utc",
                "full_day_observed", "count_scope", "quota_status") if k in data}
        quota = data.get("rate_limit", {})
        if isinstance(quota, dict):
            names = {"credits_limit", "credits_used", "credits_remaining", "daily_budget_usd", "daily_used_usd",
                     "daily_remaining_usd", "prepaid_balance_usd", "prepaid_remaining_usd", "resets_at",
                     "resets_in_seconds", "onetime_credits_balance", "onetime_credits_remaining"}
            safe["rate_limit"] = {k: v for k, v in quota.items() if k in names and isinstance(v, (int, float, str))}
        safe.setdefault("available", True)
        return safe
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"available": False, "warning": "OpenAlex source status is unavailable."}


def merged_timeline(bundle: str | Path, tail: int = 0) -> list[dict]:
    """Journal + ops events, one time-ordered stream for the raw log view."""
    items = []
    for ev in runs.journal_events(bundle):
        items.append({"t": ev.get("ts", ""), "seq": ev.get("seq", 0),
                      "source": "journal", "phase": ev.get("phase", ""),
                      "event": ev.get("event", ""),
                      "detail": _journal_detail(ev)[:500]})
    for ev in runs.ops_events(bundle):
        op = ev.get("op", "")
        kind = ev.get("kind", "")
        event = ev.get("event", "")
        text = f"op={op} kind={kind} event={event}".strip()
        items.append({"t": ev.get("at", ""), "seq": 0, "source": "ops",
                      "phase": kind or "ops", "event": event, "detail": text})
    items.sort(key=lambda i: (i["t"] or "9", i["seq"]))
    return items[-tail:] if tail > 0 else items


def acceptance_runs(root: str | Path) -> list[dict]:
    """Acceptance verdict sets (Build 17 runbook evidence), newest last."""
    out = []
    for manifest in sorted(Path(root, "acceptance").glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out.append({"date": manifest.parent.name,
                    "generated_at": data.get("generated_at", "?"),
                    "repo_rev": str(data.get("repo_rev", "?"))[:12],
                    "verdicts": [v for v in data.get("verdicts", [])
                                 if isinstance(v, dict)]})
    return out


def console_tail(bundle: str | Path, tail: int = 200) -> dict:
    path = Path(bundle) / "console.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"present": False, "lines": [],
                "note": "no console.log (captured for runs started after the tee landed)"}
    return {"present": True, "lines": lines[-tail:] if tail > 0 else lines,
            "total_lines": len(lines)}


class Handler(BaseHTTPRequestHandler):
    supervisor: Supervisor = None  # type: ignore
    server_version = "CanaryRuns/1"

    def log_message(self, fmt, *args):  # quieter than BaseHTTPRequestHandler
        pass

    def _send(self, code: int, payload, ctype="application/json", headers=None) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            elif parsed.path == "/assets/InterVariable.woff2":
                self._send(200, INTER_FONT_PATH.read_bytes(), "font/woff2")
            elif parsed.path == "/assets/research.css":
                self._send(200, RESEARCH_CSS_PATH.read_bytes(), "text/css; charset=utf-8")
            elif parsed.path == "/assets/Finlandica-Regular.woff2":
                self._send(200, FINLANDICA_FONT_PATH.read_bytes(), "font/woff2")
            elif parsed.path == "/assets/Finlandica-LICENSE.txt":
                self._send(200, FINLANDICA_LICENSE_PATH.read_bytes(), "text/plain; charset=utf-8")
            elif parsed.path == "/api/sources/openalex/status":
                self._send(200, openalex_status())
            elif re.fullmatch(r"/api/runs/[^/]+/sources/[^/]+/(?:context|file)", parsed.path):
                parts = parsed.path.split("/")
                key, source_id, action = unquote(parts[3]), unquote(parts[5]), parts[6]
                row = runs.get(key, self.supervisor.registry)
                if row is None:
                    self._send(404, {"error": "Unknown run"})
                elif action == "context":
                    self._send(200, source_context(row.get("bundle", ""), source_id,
                               query.get("paragraph_id", [""])[0], query.get("scope", ["section"])[0], key))
                else:
                    reference, original, _, _ = source_artifacts(row.get("bundle", ""), source_id)
                    if original is None:
                        self._send(404, {"error": "Saved original source file is missing"})
                    else:
                        kind = {".pdf": "application/pdf", ".xml": "application/xml", ".html": "text/html; charset=utf-8",
                                ".txt": "text/plain; charset=utf-8"}.get(original.suffix.lower(), "application/octet-stream")
                        self._send(200, original.read_bytes(), kind,
                                   {"Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff"})
            elif parsed.path == "/api/log":
                self._send(200, {"runs": runs.log_rows(self.supervisor.registry, self.supervisor.root)})
            elif parsed.path == "/api/runs":
                rows = self.supervisor.reconcile()
                self._send(200, {"runs": [dict(row, research_summary=research_summary(row))
                                          for row in rows]})
            elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/log"):
                key = parsed.path[len("/api/runs/"):-len("/log")]
                self._serve_log(key, query)
            elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/research"):
                key = parsed.path[len("/api/runs/"):-len("/research")]
                row = runs.get(key, self.supervisor.registry)
                if row is None:
                    self._send(404, {"error": f"unknown run {key}"})
                else:
                    research, live = research_for_row(row, self.supervisor)
                    self._send(200, {"key": key, "live": live,
                                     "research": research})
            elif parsed.path.startswith("/api/runs/"):
                key = parsed.path[len("/api/runs/"):]
                row = next((r for r in self.supervisor.reconcile()
                            if r.get("key") == key), None)
                if row is None:
                    self._send(404, {"error": f"unknown run {key}"})
                else:
                    self._send(200, row)
            elif parsed.path == "/api/findings":
                self._send(200, self._findings())
            elif parsed.path == "/api/usage":
                self._send(200, {"usage": self._usage()})
            elif parsed.path == "/api/experiments":
                self._send(200, {"experiments": acceptance_runs(self.supervisor.root)})
            else:
                self._send(404, {"error": "not found"})
        except KeyError:
            self._send(404, {"error": "Unknown source"})
        except ValueError as e:
            self._send(400, {"error": str(e)[:300]})
        except OSError as e:
            self._send(500, {"error": str(e)[:300]})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_json()
        try:
            if parsed.path == "/api/runs":
                if body.get("kind") == "question":
                    started = self.supervisor.start_question(str(body.get("question", "")),
                              str(body.get("name", "")), str(body.get("brief", "")))
                else:
                    started = self.supervisor.start(
                        str(body.get("name", "")), str(body.get("brief", "")),
                        body.get("argv", []), env=body.get("env"))
                self._send(201, started)
            elif parsed.path.endswith("/pause") and parsed.path.startswith("/api/runs/"):
                key = parsed.path[len("/api/runs/"):-len("/pause")]
                self._send(200, self.supervisor.pause(key))
            elif parsed.path.endswith("/unpause") and parsed.path.startswith("/api/runs/"):
                key = parsed.path[len("/api/runs/"):-len("/unpause")]
                self._send(200, self.supervisor.unpause(key))
            elif parsed.path.endswith("/rerun") and parsed.path.startswith("/api/runs/"):
                key = parsed.path[len("/api/runs/"):-len("/rerun")]
                self._send(201, self.supervisor.rerun(
                    key, str(body.get("name", "")), str(body.get("brief", ""))))
            else:
                self._send(404, {"error": "not found"})
        except KeyError as e:
            self._send(404, {"error": f"unknown run {e}"})
        except (ValueError, RuntimeError) as e:
            self._send(400, {"error": str(e)[:300]})

    def _serve_log(self, key, query) -> None:
        row = runs.get(key, self.supervisor.registry)
        if row is None:
            self._send(404, {"error": f"unknown run {key}"})
            return
        try:
            tail = int(query.get("tail", ["500"])[0] or 500)
        except ValueError:
            tail = 500
        stream = query.get("stream", ["all"])[0]
        bundle = row.get("bundle", "")
        payload: dict = {"key": key, "live": False}
        if row.get("status") in ("running", "paused") and row.get("container"):
            payload["live"] = self.supervisor.inspect(row["container"])["live"]
        if stream in ("all", "timeline"):
            payload["timeline"] = merged_timeline(bundle, tail)
        if stream in ("all", "console"):
            payload["console"] = console_tail(bundle, tail)
        if stream == "all":
            launcher = Path(str(bundle) + ".launcher.log")
            try:
                lines = launcher.read_text(encoding="utf-8",
                                           errors="replace").splitlines()
                payload["launcher"] = {"present": True, "lines": lines[-tail:]}
            except OSError:
                payload["launcher"] = {"present": False, "lines": []}
        self._send(200, payload)

    def _findings(self) -> dict:
        now = time.monotonic()
        cache = self.supervisor.findings_cache
        if now - cache["at"] < FINDINGS_TTL_S and cache["data"] is not None:
            return cache["data"]
        data: dict = {"issues": [], "prs": [], "error": ""}
        try:
            root = str(self.supervisor.root)
            issues = subprocess.run(
                ["gh", "issue", "list", "--state", "open", "--limit", "50",
                 "--json", "number,title,labels,updatedAt"], cwd=root,
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            prs = subprocess.run(
                ["gh", "pr", "list", "--state", "open", "--limit", "50",
                 "--json", "number,title,headRefName,updatedAt"], cwd=root,
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            if issues.returncode == 0:
                data["issues"] = json.loads(issues.stdout or "[]")
            if prs.returncode == 0:
                data["prs"] = json.loads(prs.stdout or "[]")
            if issues.returncode != 0 or prs.returncode != 0:
                data["error"] = "gh query partially failed"
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            data["error"] = f"gh unavailable: {e}"[:200]
        self.supervisor.findings_cache = {"at": now, "data": data}
        return data

    def _usage(self) -> list[dict]:
        rows = []
        for row in runs.load(self.supervisor.registry):
            usage = row.get("usage", {}) or {}
            rows.append({"key": row.get("key"), "name": row.get("name"),
                         "status": row.get("status"),
                         "model_calls": usage.get("model_calls", 0),
                         "tokens": usage.get("tokens", 0)})
        return rows


def make_server(port: int = DEFAULT_PORT, registry: str | Path = runs.REGISTRY,
                root: str | Path = runs.ROOT) -> ThreadingHTTPServer:
    """Loopback-bound server (port 0 picks an ephemeral port for tests)."""
    handler = type("RunsHandler", (Handler,), {})
    handler.supervisor = Supervisor(registry, root)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def serve(port: int = DEFAULT_PORT, registry: str | Path = runs.REGISTRY,
          root: str | Path = runs.ROOT) -> None:
    """Bind loopback and serve forever. Runs outlive this process by design."""
    server = make_server(port, registry, root)
    print(f"runs dashboard serving at http://127.0.0.1:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
