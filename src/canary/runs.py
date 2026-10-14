"""Run registry: every run gets a name, a brief, and one dashboard row.

Registry lives at runs/registry.json (local-only, gitignored). The daemon
and launcher register container runs at start/finish; adopt() takes in
older bundles. Writes are atomic (tmp + replace); reads tolerate a
missing file. Bundle parsing is defensive: missing files yield empty
lists and "unknown" fields, never an exception.
"""
from __future__ import annotations

import glob
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
# Overridable so tests and alternate checkouts isolate their registry; the
# daemon exports it for launcher children so hooks land in the same file.
REGISTRY = Path(os.environ.get("CANARY_RUNS_REGISTRY", ROOT / "runs" / "registry.json"))
# Operator env passthrough for launches (advanced mode; default runs need
# none of it). Daemon-managed vars (OUT, RUN_*, registry) are never passed.
LAUNCH_ENV_KEYS = ("CANARY_STAGE", "CANARY_TIMEOUT_S", "CANARY_WHEELS")
LAUNCH_ENV_MAXLEN = 2000


def sanitize_launch_env(env) -> dict:
    """Allowlisted launch env or ValueError; the launcher rechecks semantics."""
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ValueError("env must be an object of KEY: value strings")
    clean = {}
    for key, value in env.items():
        if key not in LAUNCH_ENV_KEYS:
            raise ValueError(f"env key not allowed: {key!r}")
        if not isinstance(value, str) or len(value) > LAUNCH_ENV_MAXLEN:
            raise ValueError(f"env value for {key} must be a string <= 2000 chars")
        clean[key] = value
    return clean


def ambient_launch_env() -> dict:
    """Allowlisted keys from this process's environment (launcher hook)."""
    return {k: v for k in LAUNCH_ENV_KEYS if (v := os.environ.get(k))}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(path: str | Path = REGISTRY) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


@contextmanager
def _registry_lock(path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _save_locked(rows: list[dict], path: str | Path) -> None:
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(rows, indent=1) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save(rows: list[dict], path: str | Path = REGISTRY) -> None:
    with _registry_lock(path):
        _save_locked(rows, path)


def _upsert_locked(row: dict, path: str | Path) -> dict:
    rows = [r for r in load(path) if r.get("key") != row["key"]]
    rows.append(row)
    rows.sort(key=lambda r: str(r.get("started_at", "")))
    _save_locked(rows, path)
    return row


def upsert(row: dict, path: str | Path = REGISTRY) -> dict:
    with _registry_lock(path):
        return _upsert_locked(row, path)


def get(key: str, path: str | Path = REGISTRY) -> dict | None:
    return next((r for r in load(path) if r.get("key") == key), None)


def register_start(key: str, name: str, brief: str, kind: str = "?",
                   bundle: str = "", container: str = "", launch: str = "",
                   path: str | Path = REGISTRY,
                   launch_argv: list[str] | None = None,
                   launch_env: dict | None = None,
                   status: str = "running") -> dict:
    return upsert({"key": key, "name": name, "brief": brief, "run_id": "?",
                   "kind": kind, "status": status, "started_at": utcnow(),
                   "ended_at": None, "bundle": bundle, "container": container,
                   "launch": launch, "launch_argv": list(launch_argv or []),
                   "launch_env": dict(launch_env or {}),
                   "seed": "?", "profile": "?",
                   "maintenance": False, "model": "?", "usage": {},
                   "iterations": [], "improvements": [], "tally": {}}, path)


_NON_TERMINAL = (None, "unknown", "starting", "running", "paused", "")


def register_finish(key: str, status: str = "done", bundle: str = "",
                    container: str = "", path: str | Path = REGISTRY) -> dict | None:
    with _registry_lock(path):
        rows = load(path)
        current = next((r for r in rows if r.get("key") == key), None)
        if current is None:
            return None
        if current.get("status") not in _NON_TERMINAL:
            return current  # terminal rows are immutable; late writers no-op
        target = Path(bundle or current.get("bundle", ""))
        if target.is_dir():
            parsed = parse_bundle(target)
            current.update({k: v for k, v in parsed.items()
                            if k != "bundle" and v not in (None, "?", {})})
            current["bundle"] = str(target)
        if status != "done" or current.get("status") in _NON_TERMINAL:
            current["status"] = status  # failed/interrupted win; done defers to bundle
        current["ended_at"] = utcnow()
        current["ended_at_estimated"] = False
        if container:
            current["container"] = container
        return _upsert_locked(current, path)


def adopt_bundle(bundle: str | Path, name: str = "", brief: str = "",
                 path: str | Path = REGISTRY) -> dict:
    bundle = Path(bundle)
    parsed = parse_bundle(bundle)
    row = {"key": bundle.name,
           "name": name or f"{bundle.name} — {parsed['kind']} run",
           "brief": brief or f"Seed: {parsed['seed']} (backfilled, no brief recorded)",
           "launch": "", "container": parsed.pop("container", None)}
    row.update(parsed)
    return upsert(row, path)


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def journal_events(bundle: str | Path) -> list[dict]:
    events = []
    try:
        lines = (Path(bundle) / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def ops_events(bundle: str | Path) -> list[dict]:
    events = []
    try:
        lines = (Path(bundle) / "ops.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _iterations(bundle: Path, finished: bool) -> list[dict]:
    iters = []
    for path in sorted(glob.glob(str(bundle / "iterations" / "iter*.json"))):
        d = _load_json(Path(path), {})
        if not isinstance(d, dict):
            continue
        iters.append({"n": d.get("n"), "kind": d.get("kind", "?"),
                      "question": d.get("question", "?")})
    iters.sort(key=lambda i: (i["n"] is None, i["n"]))
    for pos, item in enumerate(iters):
        item["status"] = "done" if (finished or pos < len(iters) - 1) else "active"
    return iters


def _improvements(bundle: Path) -> list[dict]:
    """One entry per code-change proposal, newest evidence first.

    Candidate records carry the eval verdict; assessment proposals without
    a candidate record never reached eval (diff failed or round aborted).
    PR linkage arrives with the publish bridge (#66); until then every
    entry is honestly unfiled.
    """
    found: dict[str, dict] = {}
    for path in sorted(glob.glob(str(bundle / "assessments" / "candidates" / "*.json"))):
        d = _load_json(Path(path), {})
        if not isinstance(d, dict):
            continue
        pid = str(d.get("proposal_id", Path(path).stem))
        found[pid] = {"id": pid, "target": d.get("target", "?"),
                      "status": d.get("status", "?"),
                      "reason": str(d.get("detail", ""))[:300],
                      "pr_url": None, "merged": False}
    for path in sorted(glob.glob(str(bundle / "assessments" / "assess-*.json"))):
        d = _load_json(Path(path), {})
        if not isinstance(d, dict):
            continue
        for p in d.get("proposals", []) or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id", "?"))
            if pid in found:
                continue
            found[pid] = {"id": pid, "target": p.get("target", "?"),
                          "status": "not-evaluated",
                          "reason": str(p.get("change", ""))[:300],
                          "pr_url": None, "merged": False}
    return [found[k] for k in sorted(found)]


def _revised_tally(events: list[dict]) -> dict:
    tally = {"kept": 0, "reverted": 0, "skipped": 0}
    for ev in events:
        if ev.get("event") not in ("revised", "mid-revised"):
            continue
        for part in str(ev.get("detail", "")).replace(",", " ").split():
            if "=" not in part:
                continue
            key, _, value = part.partition("=")
            if key in tally:
                try:
                    tally[key] += int(value)
                except ValueError:
                    pass
    return tally


def parse_bundle(bundle: str | Path) -> dict:
    """Bundle directory → dashboard row fields (no registry I/O)."""
    bundle = Path(bundle)
    manifest = _load_json(bundle / "manifest.json", {})
    run = _load_json(bundle / "run.json", {})
    if not isinstance(manifest, dict):
        manifest = {}
    if not isinstance(run, dict):
        run = {}
    spec = manifest.get("spec", {})
    if not isinstance(spec, dict):
        spec = {}
    events = journal_events(bundle)
    native_question = run.get("kind") == "question"
    status = manifest.get("status") or (run.get("status") if native_question else run.get("stopped")) or "unknown"
    if not isinstance(status, str):
        status = "unknown"
    finished = status not in ("unknown", "started", "starting", "running", "in_progress", "paused", "")
    usage = run.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    record = {
        "run_id": manifest.get("run_id", "?"),
        "kind": manifest.get("kind", run.get("kind", "?")),
        "status": status,
        "started_at": manifest.get("started_at"),
        "ended_at": manifest.get("finished_at"),
        "seed": spec.get("question", run.get("seed", "?")),
        "profile": spec.get("profile", "?"),
        "maintenance": bool(spec.get("maintenance", False)),
        "model": run.get("model", "?"),
        "reasoning_effort": run.get("reasoning_effort"),
        "usage": {"model_calls": usage.get("model_calls", 0),
                  "tokens": usage.get("tokens", 0)},
        "iterations": _iterations(bundle, finished),
        "improvements": _improvements(bundle),
        "tally": _revised_tally(events),
        "bundle": str(bundle),
        "log_files": [str(bundle / name) for name in
                      ("journal.jsonl", "ops.jsonl", "console.log")
                      if (bundle / name).exists()],
    }
    if native_question:
        receipts = run.get("receipts") if isinstance(run.get("receipts"), list) else []
        tokens = sum(r.get("usage", {}).get("total_tokens", 0) for r in receipts
                     if isinstance(r, dict) and isinstance(r.get("usage"), dict)
                     and type(r["usage"].get("total_tokens", 0)) is int)
        record.update(run_id=run.get("run_id", bundle.name), kind="question",
                      seed=run.get("question", "?"), started_at=run.get("generated_at"),
                      profile="question", maintenance=False,
                      usage={"model_calls": run.get("llm_calls", 0), "tokens": tokens,
                             "keyword_requests": run.get("keyword_requests", 0),
                             "receipts_available": len(receipts)},
                      source_usage_before=run.get("usage_before", {}),
                      source_usage_after=run.get("usage_after", {}))
    receipt = _load_json(Path(str(bundle) + ".receipt.json"), {})
    if isinstance(receipt, dict) and receipt:
        record["container"] = receipt.get("NAME")
        record["exit_code"] = receipt.get("RC")
        record["git_rev"] = receipt.get("GIT_REV")
        if not manifest and not native_question:
            # Exec probes ship no canary bundle; the receipt is the record.
            record["kind"] = "exec"
            rc = receipt.get("RC")
            record["status"] = ("done" if rc == "0" else
                                "failed" if rc not in (None, "") else "unknown")
            for field, epoch in (("started_at", "STARTED"), ("ended_at", "FINISHED")):
                try:
                    record[field] = datetime.fromtimestamp(
                        int(receipt[epoch]), tz=timezone.utc).isoformat()
                except (KeyError, TypeError, ValueError):
                    pass
    record.update(_bundle_log_metrics(bundle, run, manifest, events, record))
    return record


_ACTIVE_STATUS = {None, "", "unknown", "started", "starting", "running", "in_progress", "paused"}
_FAILURE_STATUS = {"failed", "interrupted", "cancelled", "provider_blocked", "invalid_input"}
_LOG_FIELDS = ("started_at", "ended_at", "duration_seconds", "papers_covered", "papers_acquired", "answered",
               "input_tokens", "output_tokens", "cached_tokens", "timing_source",
               "ended_at_estimated", "papers_core", "papers_supporting", "target_papers", "audit_status")


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _seconds(value) -> float | None:
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        converted = float(value)
        return converted if math.isfinite(converted) else None
    except OverflowError:
        return None


def _receipt_tokens(receipts) -> dict:
    """Missing per-call counts make that aggregate unknown, never silently zero."""
    result = dict(input_tokens=None, output_tokens=None, cached_tokens=None)
    if not isinstance(receipts, list) or not receipts:
        return result
    fields = {"input_tokens": [], "output_tokens": [], "cached_tokens": []}
    for receipt in receipts:
        usage = receipt.get("usage", {}) if isinstance(receipt, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        prompt = _count(usage.get("prompt_tokens", usage.get("input_tokens")))
        output = _count(usage.get("completion_tokens", usage.get("output_tokens")))
        details = usage.get("prompt_tokens_details", usage.get("input_tokens_details", {}))
        if not isinstance(details, dict):
            details = {}
        cached = _count(usage.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
        if cached is not None and (prompt is None or cached > prompt):
            cached = None
        fields["input_tokens"].append(prompt)
        fields["output_tokens"].append(output)
        fields["cached_tokens"].append(cached)
    return {key: sum(values) if all(value is not None for value in values) else None
            for key, values in fields.items()}


def _reviewed_papers(bundle: Path, run: dict) -> int | None:
    """Distinct selected sources in legacy iteration provenance, not discovery hits."""
    iterations = run.get("iterations", [])
    if not isinstance(iterations, list):
        iterations = []
    iterations = [i for i in iterations if isinstance(i, dict)]
    # Older durable bundles keep provenance in separate iteration files.
    iterations += [d for p in sorted((bundle / "iterations").glob("iter*.json"))
                   if isinstance(d := _load_json(p, {}), dict)]
    identities = set()
    recorded = False
    sources_by_title = {}
    for iteration in iterations:
        sources = iteration.get("sources", [])
        for source in sources if isinstance(sources, list) else []:
            if isinstance(source, dict) and isinstance(source.get("title"), str):
                sources_by_title[source["title"].strip().casefold()] = source
    for iteration in iterations:
        if iteration.get("kind") != "review" or not isinstance(iteration.get("papers"), list):
            continue
        recorded = True
        for paper in iteration["papers"]:
            source = paper if isinstance(paper, dict) else sources_by_title.get(str(paper).strip().casefold(), {})
            if isinstance(source, dict):
                identity = source.get("ref") or source.get("doi") or source.get("id") or source.get("title")
            else:
                identity = None
            # The old writer persisted the selected paper titles as its source records.
            identity = identity or (paper if isinstance(paper, str) else None)
            if isinstance(identity, str) and identity.strip():
                identities.add(identity.strip().casefold())
    return len(identities) if recorded else None


def _has_answer(bundle: Path, run: dict, status: str) -> bool:
    if run.get("execution_mode") == "coverage-guided full-paper research":
        answer = _load_json(bundle / "answer.json", {})
        # Answer availability is independent of coverage target or audit status.
        return isinstance(answer, dict) and bool(answer.get("executive_summary") or answer.get("claims"))
    if status in _FAILURE_STATUS or str(status).startswith("failed"):
        return False
    if run.get("kind") == "question":
        answer = _load_json(bundle / "answer.json", {})
        return (run.get("status") == "answered" and isinstance(answer, dict)
                and bool(answer.get("executive_summary") or answer.get("claims")))
    try:
        text = (bundle / "synthesis.md").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(text) and "No final synthesis" not in text


def _bundle_log_metrics(bundle: Path, run: dict, manifest: dict,
                        events: list[dict], record: dict) -> dict:
    profile = _load_json(bundle / "profile.json", {})
    if not isinstance(profile, dict):
        profile = {}
    native = run.get("kind") == "question"
    profile_start = _timestamp(profile.get("started_at"))
    profile_end = _timestamp(profile.get("ended_at", profile.get("finished_at")))
    start = profile_start or _timestamp(record.get("started_at"))
    timing_source = "profile" if profile_start else "run" if native else "manifest"
    if start is None:
        for event in events:
            if event.get("event") == "start" and event.get("phase") in ("question", "cycle"):
                start = _timestamp(event.get("ts"))
                if start:
                    timing_source = "journal"
                    break
    if start is None and native:
        start = _timestamp(run.get("generated_at"))
        timing_source = "run"
    end = profile_end or _timestamp(record.get("ended_at")) or _timestamp(run.get("ended_at"))
    terminal = record.get("status") not in _ACTIVE_STATUS
    if end is None:
        terminal_events = {"answered", "error", "failed", "done", "completed", "sealed", "cancelled", "interrupted"}
        for event in reversed(events):
            if event.get("event") in terminal_events and event.get("phase") in ("question", "cycle", "bundle"):
                end = _timestamp(event.get("ts"))
                if end:
                    if timing_source != "profile":
                        timing_source = "journal"
                    break
    wall = _seconds(profile.get("wall_seconds"))
    estimated = False
    if end is None and terminal and start and wall is not None:
        try:
            end = start + timedelta(seconds=wall)
            estimated = True
        except OverflowError:
            pass
    duration = None
    if wall is not None and (estimated or (profile_start and profile_end and profile_end >= profile_start)):
        duration = wall
        timing_source = "profile"
    elif start and end and end >= start:
        duration = (end - start).total_seconds()
    papers = _reviewed_papers(bundle, run) if not native else None
    acquired = _count(run.get("accepted")) if native else None
    if native:
        if acquired is None:
            access = _load_json(bundle / "access.json", {})
            accepted = access.get("accepted") if isinstance(access, dict) else None
            if isinstance(accepted, list):
                acquired = len(accepted)
        graph = _load_json(bundle / "answer.json", {})
        references = graph.get("references") if isinstance(graph, dict) else None
        # Final full-answer assembly follows processing of every source. A
        # failed extraction or source-review-only replay does not establish that
        # all acquired copies were read. Duplicate source copies count once.
        if (run.get("execution_mode") != "saved source-review replay"
                and isinstance(references, list) and
                (graph.get("executive_summary") or graph.get("claims"))
                and all(isinstance(ref, dict) and isinstance(ref.get("id"), str)
                        and ref["id"] and ref.get("kind") in ("fulltext", "abstract")
                        for ref in references)):
            papers = len({ref["id"] for ref in references if ref["kind"] == "fulltext"})
    if run.get("execution_mode") == "coverage-guided full-paper research":
        # Source graph includes reviewed and cited context; only committed substantive
        # review records establish a reviewed-paper count.
        papers = _count(run.get("papers_reviewed"))
    tokens = _receipt_tokens(run.get("receipts", profile.get("receipts")))
    return {"started_at": start.isoformat() if start else None,
            "ended_at": end.isoformat() if end else None,
            "duration_seconds": duration, "timing_source": timing_source if start or end or duration is not None else None,
            "ended_at_estimated": estimated, "papers_covered": papers, "papers_acquired": acquired,
            "answered": _has_answer(bundle, run, record.get("status", "unknown")),
            "papers_core": _count(run.get("papers_core")),
            "papers_supporting": _count(run.get("papers_supporting")),
            "target_papers": _count(run.get("target_papers")), "audit_status": run.get("audit_status"), **tokens}


def log_rows(path: str | Path = REGISTRY, root: str | Path = ROOT) -> list[dict]:
    """Read saved run metrics without launcher polling or registry mutation."""
    result = []
    for row in load(path):
        bundle = Path(row["bundle"]) if row.get("bundle") else None
        if bundle and not bundle.is_absolute():
            bundle = Path(root) / bundle
        parsed = parse_bundle(bundle) if bundle and bundle.is_dir() else {}
        entry = {key: row.get(key) for key in ("key", "name", "kind", "status")}
        entry["model"] = parsed.get("model", row.get("model"))
        entry["reasoning_effort"] = parsed.get("reasoning_effort", row.get("reasoning_effort"))
        entry.update({field: parsed.get(field, row.get(field)) for field in _LOG_FIELDS})
        if entry["ended_at_estimated"] and not row.get("ended_at_estimated") and _timestamp(row.get("ended_at")):
            entry.update(ended_at=row["ended_at"], ended_at_estimated=False, duration_seconds=None)
        # Launcher timestamps remain available for bundles that have no research timing.
        for field in ("started_at", "ended_at"):
            if not _timestamp(entry.get(field)):
                value = _timestamp(row.get(field))
                entry[field] = value.isoformat() if value else None
        if entry["duration_seconds"] is None:
            start, end = _timestamp(entry["started_at"]), _timestamp(entry["ended_at"])
            if start and end and end >= start:
                entry["duration_seconds"] = (end - start).total_seconds()
                entry["timing_source"] = "registry"
        entry["answered"] = bool(entry.get("answered"))
        if str(row.get("status", "")) in _FAILURE_STATUS or str(row.get("status", "")).startswith("failed"):
            entry["answered"] = False
        entry["ended_at_estimated"] = bool(entry.get("ended_at_estimated"))
        result.append(entry)
    return sorted(result, key=lambda row: row["started_at"] or "", reverse=True)


def _research_identity(bundle: Path, run: dict | None = None) -> tuple:
    run = run if run is not None else _load_json(bundle / "run.json", {})
    if not isinstance(run, dict):
        run = {}
    manifest = _load_json(bundle / "manifest.json", {})
    if not isinstance(manifest, dict):
        manifest = {}
    kind = run.get("kind", manifest.get("kind"))
    events = journal_events(bundle)
    journal_start = next((e.get("ts") for e in events
                          if e.get("phase") == kind and e.get("event") == "start" and _timestamp(e.get("ts"))), None)
    start = (run.get("generated_at") if kind == "question" else manifest.get("started_at")) or journal_start
    if _timestamp(start):
        spec = manifest.get("spec", {})
        spec = spec if isinstance(spec, dict) else {}
        seed = run.get("question", run.get("seed", spec.get("question", "")))
        return kind, str(seed).strip(), _timestamp(start).isoformat()
    return "bundle", str(bundle.resolve())


def _research_richness(bundle: Path, run: dict) -> tuple:
    run = run if isinstance(run, dict) else {}
    files = ("answer.json", "access.json", "source-recovery.json", "profile.json", "synthesis.md", "journal.jsonl")
    answer = _load_json(bundle / "answer.json", {})
    answer_size = len(json.dumps(answer)) if isinstance(answer, dict) and answer else 0
    receipts = run.get("receipts", [])
    archived = any(word in str(bundle).lower() for word in ("archive", "backup", "before", "draft"))
    return (sum((bundle / name).is_file() for name in files), answer_size,
            len(receipts) if isinstance(receipts, list) else 0, not archived)


def import_research_bundles(root: str | Path, path: str | Path = REGISTRY) -> dict:
    """Explicit, idempotent import of executed question/cycle bundles, including failures.

    Execution timestamps identify copies. Richer artifacts win; operator keys/names
    survive. No filesystem modification time is treated as research execution time.
    """
    with _registry_lock(path):
        root = Path(root).resolve()
        rows = load(path)
        groups = {}
        skipped = 0
        for run_path in sorted(root.rglob("run.json")):
            run = _load_json(run_path, {})
            bundle = run_path.parent.resolve()
            manifest = _load_json(bundle / "manifest.json", {})
            if not isinstance(run, dict) or not isinstance(manifest, dict):
                skipped += 1
                continue
            kind = run.get("kind", manifest.get("kind"))
            events = journal_events(bundle)
            executed = any(e.get("phase") == kind and e.get("event") == "start" and _timestamp(e.get("ts")) for e in events)
            executed = executed or bool(_timestamp(manifest.get("started_at")) and manifest.get("run_id"))
            profile = _load_json(bundle / "profile.json", {})
            profile = profile if isinstance(profile, dict) else {}
            executed = executed or bool(_timestamp(run.get("generated_at")) and
                                        (run.get("receipts") or _timestamp(profile.get("started_at"))))
            if kind not in ("question", "cycle") or not executed:
                skipped += 1
                continue
            identity = _research_identity(bundle, run)
            current = groups.get(identity)
            if current is None or _research_richness(bundle, run) > _research_richness(*current):
                groups[identity] = (bundle, run)
        existing = {}
        for index, row in enumerate(rows):
            if row.get("bundle"):
                bundle = Path(row["bundle"])
                if not bundle.is_absolute():
                    bundle = Path(path).resolve().parent.parent / bundle
                existing[_research_identity(bundle)] = index
        added, refreshed = [], []
        keys = {row.get("key") for row in rows}
        for identity, (bundle, run) in sorted(groups.items(), key=lambda item: str(item[0])):
            parsed = parse_bundle(bundle)
            if identity in existing:
                index = existing[identity]
                original = rows[index]
                original_bundle = Path(original.get("bundle", ""))
                if not original_bundle.is_absolute():
                    original_bundle = Path(path).resolve().parent.parent / original_bundle
                if original_bundle.is_dir() and _research_richness(original_bundle, _load_json(original_bundle / "run.json", {})) > _research_richness(bundle, run):
                    parsed = parse_bundle(original_bundle)
                updated = dict(original)
                for field, value in parsed.items():
                    if field in _LOG_FIELDS or field == "bundle" or field not in updated or updated[field] in (None, "?", {}):
                        updated[field] = value
                if updated != original:
                    rows[index] = updated
                    refreshed.append(original["key"])
                continue
            relative = bundle.relative_to(root).as_posix()
            key = "research-" + re.sub(r"[^a-z0-9]+", "-", relative.lower()).strip("-")
            if key in keys:
                key += "-" + hashlib.sha256(relative.encode()).hexdigest()[:8]
            keys.add(key)
            row = {"key": key, "name": relative, "brief": f"Seed: {parsed['seed']} (historical research)",
                   "launch": "", "container": None, **parsed}
            rows.append(row)
            added.append(key)
        if added or refreshed:
            rows.sort(key=lambda row: str(row.get("started_at") or ""))
            _save_locked(rows, path)
        return {"added": added, "refreshed": refreshed,
                "research_executions": len(groups), "skipped": skipped}
