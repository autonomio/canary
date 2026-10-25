"""Coverage-guided native research: deterministic acquisition, solo research, solo audit.

This operator runtime uses native Codex authentication. It neither invokes the
legacy DeepSeek worker nor changes/reset its service ledger. No saved experiment
module or scientific asset is a runtime dependency.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid

import httpx

from . import coverage_protocol as protocol

WORK_ID = re.compile(r"W[1-9][0-9]{0,19}\Z")
_LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def immutable_bytes(path, value):
    """An interrupted commit may finish, but different source bytes never replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != value:
            raise ValueError("Existing canonical source differs: " + path.name)
        return
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def append(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def event(workspace, name, **fields):
    row = {"at": now(), "event": name, **fields}
    append(workspace / "runtime-progress.jsonl", row)
    print(json.dumps(row, ensure_ascii=False), flush=True)


@contextmanager
def workspace_lock(workspace, name):
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / ("." + name + ".lock")).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("question"), str) or not plan["question"].strip():
        raise ValueError("QuestionPlan requires a nonempty question")
    if "entities" in plan:
        raise ValueError("QuestionPlan uses concepts, not entities")
    for name in ("concepts", "outcomes", "queries"):
        if not isinstance(plan.get(name), list):
            raise ValueError("QuestionPlan requires typed " + name)
    if not isinstance(plan.get("conditions"), (list, dict)):
        raise ValueError("QuestionPlan requires typed conditions")
    if not isinstance(plan.get("coverage_aspects", plan.get("coverage_facets")), list):
        raise ValueError("QuestionPlan requires typed coverage aspects/facets")
    if not isinstance(plan.get("eligibility"), dict):
        raise ValueError("QuestionPlan requires eligibility criteria")
    ids, terms = set(), set()
    if not plan["queries"]:
        raise ValueError("QuestionPlan requires at least one query")
    for branch in plan["queries"]:
        identifier, search = branch.get("id"), branch.get("search")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier) or identifier in ids:
            raise ValueError("Query IDs must be unique filename-safe strings")
        if not isinstance(search, str) or not search.strip() or search in terms:
            raise ValueError("Queries must have distinct nonempty search strings")
        if any(key in branch for key in ("filter", "title.search", "abstract.search")):
            raise ValueError("Only OpenAlex full-text-inclusive search= is allowed")
        ids.add(identifier); terms.add(search)
    return plan


def operator_key(root):
    """Read only the explicitly authorized source key; never evaluate .env."""
    if os.environ.get("OPENALEX_API_KEY"):
        return os.environ["OPENALEX_API_KEY"]
    envfile = root / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            key, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and key == "OPENALEX_API_KEY":
                pieces = shlex.split(value, comments=True)
                return pieces[0] if pieces else ""
    return ""


def abstract_text(index):
    positions = {number: word for word, numbers in (index or {}).items() for number in numbers}
    return " ".join(positions[number] for number in sorted(positions))


def record_daily_search(root):
    """Operator-only usage; explicitly excludes the independently deployed gateway."""
    path = root / "out" / "coverage-openalex-usage.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute("CREATE TABLE IF NOT EXISTS searches(at TEXT NOT NULL)")
        db.execute("INSERT INTO searches VALUES(?)", (now(),))
        rows = db.execute("SELECT COUNT(*) FROM searches WHERE at LIKE ?", (now()[:10] + "%",)).fetchone()[0]
    return {"date_utc": now()[:10], "keyword_searches_today": rows,
            "count_scope": "Successful coverage operator search requests, each cursor page counted; gateway and other clients excluded."}


def _openalex_retry_after(value, timestamp):
    """Honor provider delay/date; absent or malformed headers get a short cooldown."""
    value = (value or "").strip()
    try:
        if value.isascii() and value.isdigit():
            deadline = timestamp + int(value)
            datetime.fromtimestamp(deadline, timezone.utc)
            return deadline, "retry_after"
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(timestamp, deadline.timestamp()), "retry_after"
    except (ValueError, TypeError, OverflowError):
        return timestamp + 60, "default_60_seconds"


def _openalex_backoff(root, *, deadline=None, retry_after=None, source=None):
    """Short shared DB transactions; healthy concurrent searches remain parallel."""
    path = root / "out" / "coverage-openalex-usage.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute("CREATE TABLE IF NOT EXISTS searches(at TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS provider_backoff(provider TEXT PRIMARY KEY, retry_not_before REAL NOT NULL, retry_after TEXT, retry_after_source TEXT NOT NULL)")
        if deadline is not None:
            db.execute("INSERT INTO provider_backoff VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET retry_not_before=excluded.retry_not_before, retry_after=excluded.retry_after, retry_after_source=excluded.retry_after_source WHERE excluded.retry_not_before > provider_backoff.retry_not_before",
                       ("openalex", deadline, retry_after, source))
        row = db.execute("SELECT retry_not_before,retry_after,retry_after_source FROM provider_backoff WHERE provider=?", ("openalex",)).fetchone()
    if row is None:
        return None
    return {"provider": "openalex", "retry_not_before": datetime.fromtimestamp(row[0], timezone.utc).isoformat(),
            "retry_not_before_epoch": row[0], "retry_after": row[1], "retry_after_source": row[2],
            "active": row[0] > time.time(), "scope": "coverage_operator_shared"}


def _commit_discovery(workspace, works, progress, plan):
    """Commit usable candidates even when the provider pauses remaining branches."""
    progress["unique_candidates"] = len(works)
    progress["pending_query_ids"] = [identifier for identifier, state in progress["queries"].items() if not state.get("complete", False)]
    progress["complete"] = not progress["pending_query_ids"]
    if progress.get("backoff"):
        progress["backoff"]["active"] = progress["backoff"]["retry_not_before_epoch"] > time.time()
    progress["status"] = "complete" if progress["complete"] else "backoff" if progress.get("backoff", {}).get("active") else "incomplete"
    if progress["complete"]:
        progress.pop("incomplete_reason", None)
    write(workspace / "works.json", list(works.values()))
    candidates = [{"id": ident, "title": w.get("display_name") or w.get("title"), "year": w.get("publication_year"),
        "doi": w.get("doi"), "abstract": abstract_text(w.get("abstract_inverted_index")),
        "oa_indicated": (w.get("open_access") or {}).get("is_oa"), "query_ids": w["query_ids"], "rank": w["fresh_search_rank"]}
        for ident, w in works.items()]
    temporary = workspace / "candidates.jsonl.tmp"
    temporary.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in candidates))
    temporary.replace(workspace / "candidates.jsonl")
    (workspace / "titles.tsv").write_text("".join(f'{c["id"]}\t{c["year"]}\t{c["title"]}\n' for c in candidates))
    write(workspace / "search-progress.json", progress)


def discover(workspace, plan_path, *, root=None, pages=5, client=None):
    """Fresh indexed search, saving each page and exposing declared truncation."""
    workspace = Path(workspace).resolve()
    root = Path(root or Path(__file__).resolve().parents[2])
    plan = validate_plan(read(Path(plan_path)))
    if pages < 1:
        raise ValueError("Search page limit must be positive")
    with workspace_lock(workspace, "discovery"):
        previous_plan = read(workspace / "question-plan.json")
        run_question = read(workspace / "run.json", {}).get("question")
        if run_question and run_question != plan["question"]:
            raise ValueError("QuestionPlan differs from this run question")
        if previous_plan and previous_plan["question"] != plan["question"]:
            raise ValueError("Workspace belongs to a different question")
        works = {w["id"].rsplit("/", 1)[-1]: w for w in read(workspace / "works.json", [])}
        for rank, work in enumerate(works.values(), 1):
            work.setdefault("fresh_search_rank", rank)
            work.setdefault("query_ids", [])
        requests = read(workspace / "openalex-requests.json", [])
        progress = read(workspace / "search-progress.json", {"queries": {}})
        # Supplemental plans cannot erase committed search history. Restore
        # omitted branches from their saved definitions and resume their cursors.
        known = {q["id"] for q in plan["queries"]}
        prior_queries = {q["id"]: q for q in (previous_plan or {}).get("queries", [])}
        for identifier, state in progress["queries"].items():
            if identifier not in known:
                plan["queries"].append(prior_queries.get(identifier, {"id": identifier, "search": state["search"]}))
                known.add(identifier)
        for query in plan["queries"]:
            state = progress["queries"].get(query["id"])
            if state and state["search"] != query["search"]:
                raise ValueError("Change a query ID when changing its search string")
        # Rejected query redefinitions must not replace the saved plan paired
        # with committed candidates and cursor receipts.
        write(workspace / "question-plan.json", plan)
        for query in plan["queries"]:
            progress["queries"].setdefault(query["id"], {"search": query["search"], "pages": 0, "cursor": "*", "complete": False})
        headers = {"User-Agent": "Canary/coverage-native", "Cache-Control": "no-cache", "Pragma": "no-cache"}
        key = operator_key(root)
        if key:
            headers["Authorization"] = "Bearer " + key
        own = client is None
        client = client or httpx.Client(timeout=35, trust_env=False, follow_redirects=False)
        stopped_for_backoff = False
        try:
            for query in plan["queries"]:
                state = progress["queries"][query["id"]]
                if state.get("complete"):
                    continue  # Only receipts in this research workspace may be resumed.
                seen = set()
                for _ in range(pages):
                    cursor = state["cursor"]
                    if not cursor or cursor in seen:
                        break
                    seen.add(cursor)
                    backoff = _openalex_backoff(root)
                    if backoff is not None:
                        progress["backoff"] = backoff
                    if backoff and backoff["active"]:
                        stopped_for_backoff = True
                        progress["incomplete_reason"] = "openalex_rate_limited"
                        event(workspace, "search_backoff", query_id=query["id"], retry_not_before=backoff["retry_not_before"], scope=backoff["scope"])
                        break
                    if progress.get("incomplete_reason") == "openalex_rate_limited":
                        progress.pop("incomplete_reason", None)
                    started = time.monotonic()
                    try:
                        response = client.get("https://api.openalex.org/works",
                            params={"search": query["search"], "per_page": 200, "cursor": cursor,
                                    "select": "id,doi,display_name,title,publication_year,abstract_inverted_index,open_access,primary_location,best_oa_location,locations,authorships,ids,type"}, headers=headers)
                    except httpx.HTTPError as error:
                        receipt = {"query_id": query["id"], "search": query["search"], "page": state["pages"] + 1,
                            "requested_at": now(), "status": None, "error": type(error).__name__, "local_cache_used": False}
                        requests.append(receipt); write(workspace / "openalex-requests.json", requests)
                        state.update(complete=False, truncated=True, failure=receipt["error"])
                        progress["queries"][query["id"]] = state; progress["complete"] = False
                        write(workspace / "search-progress.json", progress)
                        event(workspace, "search_failed", query_id=query["id"], error=receipt["error"])
                        break
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        deadline, delay_source = _openalex_retry_after(retry_after, time.time())
                        progress["backoff"] = _openalex_backoff(root, deadline=deadline, retry_after=retry_after, source=delay_source)
                        progress["incomplete_reason"] = "openalex_rate_limited"
                        stopped_for_backoff = True
                    receipt = {"query_id": query["id"], "search": query["search"], "page": state["pages"] + 1,
                        "requested_at": now(), "status": response.status_code, "url": str(response.request.url),
                        "seconds": round(time.monotonic() - started, 3), "local_cache_used": False,
                        "rate_headers": {k: v for k, v in response.headers.items() if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"}}
                    requests.append(receipt); write(workspace / "openalex-requests.json", requests)
                    if response.status_code != 200:
                        state.update(failure_status=response.status_code, complete=False, truncated=True)
                        progress["queries"][query["id"]] = state; progress["complete"] = False
                        write(workspace / "search-progress.json", progress)
                        event(workspace, "search_failed", query_id=query["id"], status=response.status_code)
                        break  # A 429 stops the whole net; unrelated failures leave other branches usable.
                    # A successful upstream search is billable even if local
                    # metadata interpretation subsequently fails.
                    progress["keyword_requests"] = sum(r["status"] == 200 for r in requests)
                    progress["usage"] = record_daily_search(root)
                    write(workspace / "search-progress.json", progress)
                    body = response.json()
                    write(workspace / "search-responses" / f'{query["id"]}-{state["pages"] + 1:04}.json', body)
                    for work in body.get("results", []):
                        identifier = work["id"].rsplit("/", 1)[-1]
                        if not WORK_ID.fullmatch(identifier):
                            raise ValueError("Invalid OpenAlex work ID")
                        if identifier in works:
                            if query["id"] not in works[identifier]["query_ids"]:
                                works[identifier]["query_ids"].append(query["id"])
                        else:
                            works[identifier] = {**work, "query_ids": [query["id"]], "fresh_search_rank": len(works) + 1}
                    state.update(pages=state["pages"] + 1, cursor=body.get("meta", {}).get("next_cursor"),
                                 matched=body.get("meta", {}).get("count"))
                    state.pop("failure_status", None)
                    state.pop("failure", None)
                    state["complete"] = not body.get("results") or not state["cursor"] or (
                        isinstance(state["matched"], int) and state["pages"] * 200 >= state["matched"])
                    state["truncated"] = not state["complete"]
                    progress["queries"][query["id"]] = state
                    _commit_discovery(workspace, works, progress, plan)
                    event(workspace, "search_page", query_id=query["id"], page=state["pages"], candidates=len(works), complete=state["complete"])
                    if state["complete"]:
                        break
                if stopped_for_backoff:
                    break
        finally:
            if own:
                client.close()
        _commit_discovery(workspace, works, progress, plan)
    return progress


class LoggedTransport(httpx.BaseTransport):
    def __init__(self, workspace, transport=None):
        self.workspace = workspace
        self.transport = transport or httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        request.headers["Cache-Control"] = "no-cache"
        request.headers["Pragma"] = "no-cache"
        started = time.monotonic()
        try:
            response = self.transport.handle_request(request)
        except Exception as error:
            append(self.workspace / "public-network.jsonl", {"at": now(), "url": str(request.url), "error": type(error).__name__, "previous_body_used": False})
            raise
        append(self.workspace / "public-network.jsonl", {"at": now(), "url": str(request.url), "status": response.status_code,
            "seconds": round(time.monotonic() - started, 3), "previous_body_used": False})
        return response

    def close(self):
        self.transport.close()


def canonical_passages(text, reference_id):
    """Every verbatim is exactly reproducible as canonical_text[start:end]."""
    rows, page = [], None
    # Trailing horizontal whitespace can encode an empty final table cell.
    # Consume it within this paragraph instead of searching into the next one.
    for match in re.finditer(r"\S(?:.*?\S)?[^\S\n]*(?=\n\s*\n|\s*\Z)", text, re.S):
        start, end = match.span()
        marker = re.search(r"\[PDF page (\d+)\]", match.group())
        if marker:
            page = int(marker[1])
        cuts = [start]
        while end - cuts[-1] > 4000:
            split = text.rfind("\n", cuts[-1] + 1000, cuts[-1] + 4000)
            cuts.append(split + 1 if split > cuts[-1] else cuts[-1] + 4000)
        cuts.append(end)
        for a, b in zip(cuts, cuts[1:]):
            rows.append({"id": f"{reference_id}-p{len(rows) + 1:04}", "reference_id": reference_id,
                         "page": page, "start": a, "end": b, "text": text[a:b]})
    return rows


def _fetch_public(work, transport):
    from .coverage_sources import public_work_download
    return public_work_download(work, transport, socket.getaddrinfo)


def _extract(raw, kind, work):
    from .coverage_sources import _pdf_text, _xml_text, article_text
    if kind == "application/pdf":
        text, pages, blank = _pdf_text(raw)
        return text, pages, list(blank), "pdf"
    if kind in ("application/xml", "text/xml", "application/tei+xml"):
        return _xml_text(raw), 0, [], "xml"
    if kind == "text/html":
        return article_text(raw, {"doi": work.get("doi"), "title": work.get("display_name")}), 0, [], "html"
    raise ValueError("Response is not a supported paper body")


def catalog(workspace):
    rows = []
    for path in sorted((Path(workspace) / "download-receipts").glob("*.json")):
        receipt = read(path)
        if receipt.get("reference"):
            rows.append(receipt["reference"])
    return rows


def acquire(workspace, ids, *, workers=12, retry_failed=False, fetch=None, extract=None, transport=None):
    """Only explicitly selected works, concurrent failures contained, receipts committed individually."""
    workspace = Path(workspace).resolve()
    ids = list(dict.fromkeys(ids))
    if not ids or any(not WORK_ID.fullmatch(identifier) for identifier in ids):
        raise ValueError("Provide selected OpenAlex work IDs")
    if not 1 <= workers <= 32:
        raise ValueError("Acquisition workers must be between 1 and 32")
    works = {w["id"].rsplit("/", 1)[-1]: w for w in read(workspace / "works.json", [])}
    if any(identifier not in works for identifier in ids):
        raise ValueError("Selected ID is absent from this workspace's fresh search")
    fetch, extract = fetch or _fetch_public, extract or _extract
    transport = transport or LoggedTransport(workspace)
    with workspace_lock(workspace, "acquisition"):
        def one(identifier):
            path = workspace / "download-receipts" / (identifier + ".json")
            old = read(path)
            if old and (old.get("reference") or not retry_failed):
                return old
            work, started = works[identifier], time.monotonic()
            receipt = {"work_id": identifier, "title": work.get("display_name"), "started_at": now(),
                       "fresh_download": True, "previous_run_body_used": False}
            try:
                result, timeout_seen = fetch(work, transport)
                if result is None:
                    receipt.update(status="unavailable", public_timeout_seen=timeout_seen)
                else:
                    raw, kind, url = result
                    text, pages, blank, extension = extract(raw, kind, work)
                    text = text.replace("\r\n", "\n").replace("\r", "\n")
                    if len(text.strip()) < 500:
                        raise ValueError("Too little source text for paper investigation")
                    body_sha, text_sha = hashlib.sha256(raw).hexdigest(), hashlib.sha256(text.encode()).hexdigest()
                    original = workspace / "papers" / (identifier + "." + extension)
                    text_path = workspace / "texts" / (identifier + ".txt")
                    passage_path = workspace / "passages" / (identifier + ".jsonl")
                    immutable_bytes(original, raw)
                    immutable_bytes(text_path, text.encode("utf-8"))
                    immutable_bytes(passage_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in canonical_passages(text, identifier)).encode("utf-8"))
                    preview = bool(re.search(r"article[- ]preview|section snippets|access through your organization|purchase this article|preview of subscription content", text, re.I))
                    reference = {"id": identifier, "title": work.get("display_name"), "doi": work.get("doi"),
                        "year": work.get("publication_year"), "authors": [a.get("author", {}).get("display_name") for a in work.get("authorships", [])],
                        "url": url, "file": str(original.relative_to(workspace)), "text_file": str(text_path.relative_to(workspace)),
                        "content_kind": kind, "body_sha256": body_sha, "text_sha256": text_sha,
                        "pages": pages, "blank_pages": blank, "text_chars": len(text), "fresh_download_at": now(),
                        "preview_marker_detected": preview,
                        "full_text_status": "preview_suspected" if preview else "requires_source_verification"}
                    receipt.update(status="source_retrieved", reference=reference, url=url, bytes=len(raw))
            except Exception as error:
                receipt.update(status="retrieval_or_extraction_failed", reason=type(error).__name__ + ": " + str(error))
            receipt.update(ended_at=now(), seconds=round(time.monotonic() - started, 3))
            if old:
                write(workspace / "download-history" / f"{identifier}-{uuid.uuid4().hex}.json", old)
            write(path, receipt)
            event(workspace, "paper_acquisition_finished", work_id=identifier, status=receipt["status"], seconds=receipt["seconds"])
            return receipt
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = [future.result() for future in as_completed([pool.submit(one, identifier) for identifier in ids])]
        finally:
            transport.close()
        references = catalog(workspace)
        write(workspace / "references.json", references)
        (workspace / "catalog.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in references))
        write(workspace / "acquisition-progress.json", {"at": now(), "batch_selected": ids,
            "batch_attempted": len(results), "sources_retrieved": len(references), "scientific_full_paper_verification": "researcher responsibility"})
    return results


def source(workspace, reference_id, *, match=None, page=None, context=2, limit=8):
    if not WORK_ID.fullmatch(reference_id):
        raise ValueError("Invalid source ID")
    if context < 0 or limit < 1:
        raise ValueError("Invalid source window")
    path = Path(workspace) / "passages" / (reference_id + ".jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    pattern = re.compile(match, re.I) if match else None
    hits = [i for i, row in enumerate(rows) if (page is None or row["page"] == page) and (pattern is None or pattern.search(row["text"]))]
    selected = sorted({j for i in hits[:limit] for j in range(max(0, i - context), min(len(rows), i + context + 1))})
    result = [rows[i] for i in selected]
    append(Path(workspace) / "source-reads.jsonl", {"at": now(), "reference_id": reference_id, "passage_ids": [r["id"] for r in result]})
    return result


def bind_payload(workspace):
    """Expose mechanical provenance failures without rewriting or blocking science."""
    workspace = Path(workspace).resolve()
    raw = read(workspace / "investigation.json")
    if raw is None:
        raise ValueError("Research investigation.json is not available")
    payload = json.loads(json.dumps(raw))
    references = {r["id"]: r for r in catalog(workspace)}
    passages, issues = {}, []
    texts = {identifier: (workspace / reference["text_file"]).read_text(encoding="utf-8")
             for identifier, reference in references.items()}
    for path in (workspace / "passages").glob("*.jsonl"):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            reference = references.get(row["reference_id"])
            if reference is None:
                issues.append("Passage lacks committed source: " + row["id"]); continue
            text = texts[row["reference_id"]]
            if text[row["start"]:row["end"]] != row["text"]:
                issues.append("Verbatim slice mismatch: " + row["id"])
            passages[row["id"]] = row
    for identifier, reference in references.items():
        if hashlib.sha256((workspace / reference["file"]).read_bytes()).hexdigest() != reference["body_sha256"]:
            issues.append("Original source hash mismatch: " + identifier)
        if hashlib.sha256((workspace / reference["text_file"]).read_bytes()).hexdigest() != reference["text_sha256"]:
            issues.append("Canonical source hash mismatch: " + identifier)
    def require(ids, available, kind, owner):
        for identifier in ids:
            if identifier not in available:
                issues.append(f"{owner}: unknown {kind} {identifier}")
    claims = {c["id"]: c for c in raw.get("claims", [])}
    evidence = {e["id"]: e for e in raw.get("evidence", [])}
    results = {r["id"]: r for r in raw.get("study_results", [])}
    for kind, rows, indexed in (("claim", raw.get("claims", []), claims), ("evidence", raw.get("evidence", []), evidence), ("result", raw.get("study_results", []), results)):
        if len(rows) != len(indexed):
            issues.append("Duplicate " + kind + " ID")
    for claim in raw.get("claims", []):
        require(claim.get("evidence_ids", []), evidence, "evidence", claim["id"])
        require(claim.get("study_result_ids", []), results, "result", claim["id"])
        if not claim.get("evidence_ids"):
            issues.append("Claim lacks evidence: " + claim["id"])
        if 'freshness' in claim:
            from .claim_freshness import issues as freshness_issues
            issues.extend(claim['id'] + ': ' + reason for reason in freshness_issues(claim['freshness']))
        uncertainty = claim.get("uncertainty", {})
        if {"low": 1, "moderate": 2, "high": 3}.get(uncertainty.get("class")) != uncertainty.get("score"):
            issues.append("Uncertainty class/score mismatch: " + claim["id"])
    for summary in raw.get("executive_summary", []):
        require(summary.get("claim_ids", []), claims, "claim", "executive_summary")
        if not summary.get("claim_ids"):
            issues.append("Executive assertion lacks claim binding")
    for item in payload.get("evidence", []):
        require(item.get("passage_ids", []), passages, "passage", item["id"])
        item["verbatims"] = [passages[p] for p in item.get("passage_ids", []) if p in passages]
        item["reference_ids"] = list(dict.fromkeys(p["reference_id"] for p in item["verbatims"]))
        if not item.get("passage_ids"):
            issues.append("Evidence lacks verbatim: " + item["id"])
    for result in raw.get("study_results", []):
        require(result.get("reference_ids", []), references, "reference", result["id"])
        for name, field in result.get("fields", {}).items():
            if field is not None:
                require(field.get("passage_ids", []), passages, "passage", result["id"] + "." + name)
                if not field.get("passage_ids"):
                    issues.append("Result field lacks passage: " + result["id"] + "." + name)
                if any(passages.get(p, {}).get("reference_id") not in result.get("reference_ids", []) for p in field.get("passage_ids", [])):
                    issues.append("Result field uses undeclared reference: " + result["id"] + "." + name)
    for kind in ("contradictions", "gaps", "residue"):
        for record in raw.get(kind, []):
            require(record.get("claim_ids", []), claims, "claim", record["id"])
            require(record.get("evidence_ids", []), evidence, "evidence", record["id"])
    for question in raw.get("next_questions", []):
        require(question.get("gap_ids", []), {r["id"] for r in raw.get("gaps", [])}, "gap", question["id"])
        require(question.get("residue_ids", []), {r["id"] for r in raw.get("residue", [])}, "residue", question["id"])
    reviews = read(workspace / "reviewed-papers.json", [])
    selected = set(read(workspace / "selection.json", {}).get("selected_for_review_ids", []))
    admitted, seen_publications, seen_bodies = [], set(), set()
    for review in reviews:
        identifier = review.get("work_id")
        reference = references.get(identifier)
        require(review.get("passage_ids_read", []), passages, "passage", str(identifier))
        eligible = (identifier in selected and reference is not None and not reference.get("preview_marker_detected")
                    and reference.get("full_text_status") != "preview_suspected" and review.get("eligibility") in ("core", "supporting")
                    and review.get("passage_ids_read") and review.get("substantive_review_note", "").strip()
                    and review.get("investigated_sections"))
        if not eligible:
            continue
        if any(passages.get(p, {}).get("reference_id") != identifier for p in review["passage_ids_read"]):
            issues.append("Review uses another paper's passage: " + identifier); continue
        doi = (reference.get("doi") or "").lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
        identity = doi or reference["body_sha256"]
        if identity in seen_publications or reference["body_sha256"] in seen_bodies:
            issues.append("Duplicate publication counted once: " + identifier); continue
        seen_publications.add(identity); seen_bodies.add(reference["body_sha256"]); admitted.append(review)
    minimum = read(workspace / "run.json", {}).get("minimum_papers", 50)
    scarcity = read(workspace / "scarcity.json")
    accounting = {"sources_retrieved": len(references), "review_records": len(reviews),
        "substantively_reviewed_publications": len(admitted), "core_publications": sum(r["eligibility"] == "core" for r in admitted),
        "supporting_publications": sum(r["eligibility"] == "supporting" for r in admitted),
        "minimum_papers": minimum, "minimum_met": len(admitted) >= minimum,
        "scarcity_statement": scarcity, "scarcity_independently_certified": False,
        "study_family_ids": sorted({s for r in admitted for s in r.get("related_study_ids", [])}),
        "count_scope": "Recorded substantive source reading with criteria, fixed spans and notes; scientific roles and complete-paper status remain audited judgments."}
    if not accounting["minimum_met"] and not scarcity:
        issues.append("Minimum-paper target unmet without a recorded scarcity statement")
    payload.update(question=read(workspace / "question-plan.json")["question"], question_plan=read(workspace / "question-plan.json"),
                   references=list(references.values()), selection=read(workspace / "selection.json"),
                   reviewed_papers=reviews, coverage_map=read(workspace / "coverage-map.json"), accounting=accounting)
    mechanical = {"at": now(), "pass": not issues, "issues": issues,
                  "scope": "Identity/span/hash/graph/record checks; does not certify scientific interpretation, relevance or population coverage."}
    payload["raw_investigation_sha256"] = hashlib.sha256((workspace / "investigation.json").read_bytes()).hexdigest()
    payload["mechanical_audit"] = mechanical
    write(workspace / "mechanical-audit.json", mechanical)
    write(workspace / "payload.json", payload)
    return payload


def native_usage(thread_id, codex_home=None):
    """Missing operational evidence remains unknown and never destroys research."""
    try:
        return _native_usage(thread_id, codex_home)
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        return {"available": False, "reason": type(error).__name__ + ": " + str(error)}


def _native_usage(thread_id, codex_home=None):
    """Sum actual unique response receipts for this one scientific session only."""
    codex_home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    databases = list(codex_home.glob("state_*.sqlite"))
    if not databases:
        return {"available": False, "reason": "Native receipt database unavailable"}
    database = max(databases, key=lambda p: int(p.stem.split("_")[-1]))
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT rollout_path,model,reasoning_effort FROM threads WHERE id=?", (thread_id,)).fetchone()
        children = connection.execute("SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id=?", (thread_id,)).fetchall()
    if row is None:
        return {"available": False, "reason": "Scientific thread receipt not found"}
    fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    receipts, seen, contexts, incomplete = [], set(), {}, 0
    with Path(row[0]).open() as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                incomplete += 1; continue
            data = record.get("payload", {})
            if record.get("type") == "turn_context":
                contexts[data.get("turn_id")] = (data.get("model"), data.get("effort", data.get("reasoning_effort")))
            if record.get("type") != "token_usage_record" or data.get("thread_id", data.get("session_id")) != thread_id:
                continue
            response_id = data.get("response_id")
            if not response_id or response_id in seen:
                continue
            seen.add(response_id)
            model, effort = contexts.get(data.get("turn_id"), row[1:])
            receipts.append({"response_id": response_id, "thread_id": thread_id, "model": model, "reasoning_effort": effort,
                             "usage": {key: data.get("usage", {}).get(key) for key in fields}})
    totals = {}
    for field in fields:
        values = [receipt["usage"][field] for receipt in receipts]
        totals[field] = sum(values) if values and all(type(v) is int and v >= 0 for v in values) else None
    def subtract(a, b):
        return a - b if type(a) is int and type(b) is int and 0 <= b <= a else None
    totals["uncached_input_tokens"] = subtract(totals["input_tokens"], totals["cached_input_tokens"])
    totals["other_output_tokens"] = subtract(totals["output_tokens"], totals["reasoning_output_tokens"])
    return {"available": bool(receipts), "thread_id": thread_id, "response_count": len(receipts),
        "totals": totals, "receipts": receipts, "child_thread_ids": [r[0] for r in children],
        "no_subagents_verified": not children, "incomplete_lines": incomplete,
        "count_scope": "Unique native per-response receipts owned by this scientific thread; cached input/reasoning output are subsets, not extra tokens."}


def command_line(executable, model, effort, workspace, shape, output):
    return [executable, "exec", "--ignore-user-config",
            "-c", 'cli_auth_credentials_store="file"', "--disable", "multi_agent", "--disable", "multi_agent_v2",
        "-m", model, "-c", f'model_reasoning_effort="{effort}"', "-c", "agents.enabled=false",
        "-c", "features.memories=false", "-c", "features.plugins=false", "-c", "features.apps=false",
        "-c", 'web_search="disabled"', "--skip-git-repo-check", "--sandbox", "workspace-write", "--json",
        "--approve-for-me", "--output-schema", str(shape), "-o", str(output), "-"]


def refresh_committed_progress(workspace, role, model, effort, thread_id, started, elapsed, *, finished=False):
    """Refresh committed artifacts and exact native receipts without another model call."""
    from .coverage_export import export_bundle
    try:
        profile = read(workspace / "profile.json", {}) or {}
        roles = profile.get("roles")
        roles = dict(roles) if isinstance(roles, dict) else {
            name: record for name, record in profile.items() if name in ("research", "audit") and isinstance(record, dict)}
        for name in ("research", "audit"):
            receipt = read(workspace / (name + "-receipt.json"))
            if isinstance(receipt, dict) and receipt.get("ended_at"):
                recorded_usage = receipt.get("usage", {})
                roles[name] = dict(receipt, active=False, tokens=recorded_usage.get("totals", {}),
                                   receipts=recorded_usage.get("receipts", []))
        usage = native_usage(thread_id) if thread_id else {"available": False, "reason": "Thread not yet recorded"}
        roles[role] = {"active": not finished, "thread_id": thread_id, "model": model, "reasoning_effort": effort,
                       "started_at": started, "ended_at": now() if finished else None, "seconds": round(elapsed, 3),
                       "usage": usage, "tokens": usage.get("totals", {}), "receipts": usage.get("receipts", [])}
        write(workspace / "profile.json", {"roles": roles, "active_role": None if finished else role})
        return export_bundle(workspace)
    except Exception as error:
        event(workspace, "progress_export_failed", role=role, error=type(error).__name__ + ": " + str(error))
        return None


def scientific_session(workspace, role, model, effort, prompt, shape, *, executable, audit_timeout=None):
    started, before = now(), time.monotonic()
    schema_path, output_path = workspace / (role + "-schema.json"), workspace / (role + "-response.json")
    write(schema_path, shape)
    (workspace / (role + "-prompt.txt")).write_text(prompt)
    command = command_line(executable, model, effort, workspace, schema_path, output_path)
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    write(workspace / (role + "-invocation.json"), {"argv": command, "started_at": started,
        "model": model, "effort": effort, "cli_version": version, "subagents_enabled": False})
    event(workspace, role + "_started", model=model, reasoning_effort=effort)
    thread_id, turn_usage, timed_out, last_refresh = None, [], False, 0.0
    with (workspace / (role + "-events.jsonl")).open("w") as events, (workspace / (role + "-stderr.log")).open("w") as stderr:
        process = subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=stderr, text=True, start_new_session=True)
        def stop_audit():
            nonlocal timed_out
            timed_out = True
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            def force_stop():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            grace = threading.Timer(5, force_stop)
            grace.daemon = True
            grace.start()
        timer = threading.Timer(audit_timeout, stop_audit) if audit_timeout else None
        if timer:
            timer.start()
        try:
            process.stdin.write(prompt); process.stdin.close()
            for line in process.stdout:
                events.write(line); events.flush()
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") == "thread.started":
                    thread_id = record.get("thread_id")
                    write(workspace / (role + "-active.json"), {"thread_id": thread_id, "model": model, "effort": effort, "started_at": started})
                if record.get("type") == "turn.completed":
                    turn_usage.append(record.get("usage", {}))
                item = record.get("item", {})
                if record.get("type") == "item.completed" and item.get("type") == "agent_message":
                    event(workspace, role + "_message", text=item.get("text", "")[:1600])
                if record.get("type") in ("turn.failed", "error"):
                    event(workspace, role + "_error", detail=record)
                # Completed tools/messages expose only committed artifacts;
                # reasoning items are never published to the researcher UI.
                if record.get("type") in ("thread.started", "item.completed", "turn.completed"):
                    elapsed = time.monotonic() - before
                    if record.get("type") == "thread.started" or elapsed - last_refresh >= 10:
                        refresh_committed_progress(workspace, role, model, effort, thread_id, started, elapsed)
                        last_refresh = elapsed
            exit_code = process.wait()
        finally:
            if timer:
                timer.cancel()
    receipt = {"started_at": started, "ended_at": now(), "seconds": round(time.monotonic() - before, 3),
        "exit_code": exit_code, "thread_id": thread_id, "model": model, "reasoning_effort": effort,
        "timed_out": timed_out, "turn_usage": turn_usage,
        "usage": native_usage(thread_id) if thread_id else {"available": False, "reason": "No scientific thread ID"}}
    write(workspace / (role + "-receipt.json"), receipt)
    event(workspace, role + "_finished", seconds=receipt["seconds"], exit_code=exit_code, timed_out=timed_out)
    candidate = output_path if output_path.exists() else workspace / "audit.json"
    if role == "audit" and candidate.exists():
        try:
            read(candidate)
        except (ValueError, OSError):
            immutable_bytes(workspace / "audit-incomplete-output.json", candidate.read_bytes())
            candidate.unlink()
    if output_path.exists():
        result = read(output_path)
        write(workspace / ("investigation.json" if role == "research" else "audit.json"), result)
    elif role == "audit" and (workspace / "audit.json").exists():
        result = read(workspace / "audit.json")
        result["audit_complete"] = False
        result.setdefault("limitations", []).append("Audit stopped at its recorded time budget; intermediate audit preserved.")
        write(workspace / "audit.json", result)
    elif role == "audit":
        result = {"audit_complete": False, "claim_checks": [], "findings": [], "scores": {},
            "source_reviewed_ids": [], "selection_assessment": "Not completed", "strengths": [],
            "limitations": ["Audit did not produce a usable persisted payload"], "scope_note": "Unavailable audit is not scientific acceptance."}
        write(workspace / "audit.json", result)
    else:
        raise RuntimeError("Research session failed to produce structured final research; partial files and receipts are preserved")
    if exit_code and role == "research":
        raise RuntimeError("Research session failed; existing partial investigation remains available")
    refresh_committed_progress(workspace, role, model, effort, thread_id, started, receipt["seconds"], finished=True)
    return result, receipt


def run(question, workspace, *, minimum_papers=50, audit_seconds=600, executable=None, on_reserved=None):
    workspace = Path(workspace).resolve()
    if not question.strip() or minimum_papers < 1 or audit_seconds < 1:
        raise ValueError("Question, paper target and audit duration must be nonempty/positive")
    executable = executable or shutil.which("codex")
    if not executable:
        raise RuntimeError("Native Codex CLI is required; no provider/model fallback is configured")
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "run_coverage_research.py"
    command = shlex.join([sys_executable(), str(script)])
    workspace.mkdir(parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise ValueError("A new research run requires an empty workspace; use tools only to resume acquisition within a run")
    try:
        # Exclusive creation reserves this directory across callers/processes,
        # including after a crash. No run asset is written before reservation.
        (workspace / ".run-reservation").touch(exist_ok=False)
    except FileExistsError:
        raise ValueError("This workspace is already reserved for a research run; choose an empty workspace") from None
    if on_reserved is not None:
        on_reserved()
    started, before = now(), time.monotonic()
    manifest = {"question": question, "kind": "question", "execution_mode": "coverage-guided full-paper research",
        "status": "researching", "started_at": started, "generated_at": started,
        "selection_method": "coverage-guided", "minimum_papers": minimum_papers, "research_model": protocol.RESEARCH_MODEL,
        "research_effort": protocol.RESEARCH_EFFORT, "audit_model": protocol.AUDIT_MODEL, "audit_effort": protocol.AUDIT_EFFORT,
        "no_subagents": True, "fresh_assets_only": True, "audit_budget_seconds": audit_seconds,
        "accounting_scope": "Native CLI workload; legacy DeepSeek gateway token ledger remains a separate deployment."}
    write(workspace / "run.json", manifest)
    write(workspace / "question-plan-schema.json", protocol.question_plan_schema())
    write(workspace / "investigation-schema.json", protocol.investigation_schema())
    (workspace / "AGENTS.md").write_text("Read repository AGENTS.md first.\n" + protocol.SCIENTIFIC_RULES +
        "Use only this new research workspace's sources and current question. Never inspect saved experiments or previous answers.\n")
    try:
        scientific_session(workspace, "research", protocol.RESEARCH_MODEL, protocol.RESEARCH_EFFORT,
            protocol.research_prompt(question, command, minimum_papers), protocol.investigation_schema(), executable=executable)
        payload = bind_payload(workspace)
        manifest.update(status="auditing", research_finished_at=now(), accounting=payload["accounting"])
        write(workspace / "run.json", manifest)
        write(workspace / "audit-input.json", payload)
        audit, _ = scientific_session(workspace, "audit", protocol.AUDIT_MODEL, protocol.AUDIT_EFFORT,
            protocol.audit_prompt(question, command, audit_seconds), protocol.audit_schema(), executable=executable,
            audit_timeout=audit_seconds)
        checked = {r["claim_id"] for r in audit.get("claim_checks", [])}
        for claim in payload["claims"]:
            if claim["id"] not in checked:
                audit.setdefault("claim_checks", []).append({"claim_id": claim["id"], "assessment": "not_checked",
                    "reason": "No persisted independent check", "passage_ids": [], "reference_ids": [], "context_checked": []})
                audit["audit_complete"] = False
        write(workspace / "audit.json", audit)
        payload["independent_audit"] = audit
        payload["profile"] = {role: read(workspace / (role + "-receipt.json")) for role in ("research", "audit")}
        payload["research_status"] = "raw_research_with_separate_audit"
        write(workspace / "payload.json", payload)
        write(workspace / "profile.json", payload["profile"])
        target = "met" if payload["accounting"]["minimum_met"] else (
            "scarcity_reported_not_certified" if payload["accounting"]["scarcity_statement"] else "unmet")
        manifest.update(status="partial" if target == "unmet" else "complete", minimum_target_status=target,
                        audit_complete=audit.get("audit_complete", False), answered=bool(payload.get("claims")),
                        scientific_acceptance="See individual independent claim checks and audit limitations")
    except Exception as error:
        manifest.update(status="failed", failure=type(error).__name__ + ": " + str(error))
        raise
    finally:
        manifest.update(ended_at=now(), seconds=round(time.monotonic() - before, 3))
        write(workspace / "run.json", manifest)
    return payload


def sys_executable():
    import sys
    return sys.executable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    p = commands.add_parser("run"); p.add_argument("question"); p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--minimum-papers", type=int, default=50); p.add_argument("--audit-seconds", type=int, default=600); p.add_argument("--codex")
    p = commands.add_parser("discover"); p.add_argument("--workspace", type=Path, required=True); p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--pages", type=int, default=5)
    p = commands.add_parser("acquire"); p.add_argument("--workspace", type=Path, required=True); p.add_argument("--ids", nargs="+", required=True)
    p.add_argument("--workers", type=int, default=12); p.add_argument("--retry-failed", action="store_true")
    p = commands.add_parser("catalog"); p.add_argument("--workspace", type=Path, required=True)
    p = commands.add_parser("source"); p.add_argument("--workspace", type=Path, required=True); p.add_argument("reference_id")
    p.add_argument("--match"); p.add_argument("--page", type=int); p.add_argument("--context", type=int, default=2); p.add_argument("--limit", type=int, default=8)
    p = commands.add_parser("finalize"); p.add_argument("--workspace", type=Path, required=True)
    p = commands.add_parser("usage"); p.add_argument("thread_id")
    args = parser.parse_args(argv)
    if args.action == "run":
        result = run(args.question, args.workspace, minimum_papers=args.minimum_papers, audit_seconds=args.audit_seconds, executable=args.codex)
        print(json.dumps({"payload": str(args.workspace / "payload.json"), "claims": len(result["claims"]), "accounting": result["accounting"]}, ensure_ascii=False))
    elif args.action == "discover":
        print(json.dumps(discover(args.workspace, args.plan, pages=args.pages), ensure_ascii=False))
    elif args.action == "acquire":
        result = acquire(args.workspace, args.ids, workers=args.workers, retry_failed=args.retry_failed)
        print(json.dumps({"attempted": len(result), "sources_available": len(catalog(args.workspace))}))
    elif args.action == "catalog":
        print(json.dumps(catalog(args.workspace), ensure_ascii=False, indent=2))
    elif args.action == "source":
        print(json.dumps(source(args.workspace, args.reference_id, match=args.match, page=args.page, context=args.context, limit=args.limit), ensure_ascii=False, indent=2))
    elif args.action == "finalize":
        result = bind_payload(args.workspace)
        print(json.dumps({"accounting": result["accounting"], "mechanical_audit": result["mechanical_audit"]}, ensure_ascii=False))
    elif args.action == "usage":
        print(json.dumps(native_usage(args.thread_id), ensure_ascii=False, indent=2))
    return 0
