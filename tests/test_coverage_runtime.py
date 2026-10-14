"""Offline runtime contracts; fake acquisition/receipts are not scientific validation."""
import json
import multiprocessing
from pathlib import Path
import sqlite3
import threading
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

import httpx

from canary import coverage_protocol as protocol
from canary import coverage_runtime as runtime


def rich_plan():
    return {"question": "Which capture cues improve delayed retrieval per added minute?",
        "concepts": [{"name": "capture cues", "synonyms": ["context annotation"], "scope": "At recording"}],
        "outcomes": [{"name": "delayed retrieval", "priority": "critical", "measures": ["success"]}],
        "conditions": {"comparator": "recording/search alone"},
        "coverage_facets": [{"id": "effort", "rationale": "Measured incremental minutes"}],
        "eligibility": {"core": ["comparative delayed retrieval"], "supporting": ["cue mechanisms"]},
        "evidence_targets": {"target_full_publications": 50},
        "queries": [{"id": "q1", "search": '"context capture" AND retrieval', "facets": ["effort"]}]}


def work(identifier):
    return {"id": "https://openalex.org/" + identifier, "display_name": "Synthetic source " + identifier,
            "doi": "https://doi.org/10.999/" + identifier, "publication_year": 2026,
            "abstract_inverted_index": {"synthetic": [0], "abstract": [1]}, "query_ids": ["q1"], "fresh_search_rank": 1}


def setup_sources(tmp_path, ids=("W1", "W2", "W3")):
    runtime.write(tmp_path / "works.json", [work(identifier) for identifier in ids])
    runtime.write(tmp_path / "question-plan.json", rich_plan())
    return tmp_path


class Transport:
    def close(self):
        pass


def extraction(raw, kind, source):
    return ("[PDF page 1]\n\n" + "Relevant methods and measured results with a matched comparator. " * 15,
            1, [], "pdf")


def test_fresh_search_cap_resume_and_rich_plan_preservation(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "synthetic-source-key")
    plan_path = tmp_path / "input-plan.json"
    runtime.write(plan_path, rich_plan())
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.params["cursor"] == "*":
            return httpx.Response(200, json={"results": [work("W1")], "meta": {"count": 400, "next_cursor": "page2"}})
        return httpx.Response(200, json={"results": [work("W2")], "meta": {"count": 400, "next_cursor": None}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = runtime.discover(tmp_path / "research", plan_path, root=tmp_path, pages=1, client=client)
        assert first["complete"] is False and first["queries"]["q1"]["truncated"] is True
        second = runtime.discover(tmp_path / "research", plan_path, root=tmp_path, pages=1, client=client)
        assert second["complete"] is True
        runtime.discover(tmp_path / "research", plan_path, root=tmp_path, pages=1, client=client)
    assert len(requests) == 2  # Only same-run completed receipts are resumable.
    assert all(r.headers["Authorization"] == "Bearer synthetic-source-key" for r in requests)
    assert all("filter" not in r.url.params for r in requests)
    assert all("abstract_inverted_index" in r.url.params["select"] and "locations" in r.url.params["select"] for r in requests)
    assert runtime.read(tmp_path / "research" / "question-plan.json") == rich_plan()
    logs = (tmp_path / "research" / "openalex-requests.json").read_text()
    assert "synthetic-source-key" not in logs
    assert len(runtime.read(tmp_path / "research" / "works.json")) == 2



def test_openalex_429_stops_whole_net_and_resumes_committed_cursor(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    monkeypatch.setenv("OPENALEX_API_KEY", "synthetic-source-key")
    plan = rich_plan()
    plan["queries"].append({"id": "q2", "search": "research annotation and rediscovery"})
    plan_path = tmp_path / "input-plan.json"
    workspace = tmp_path / "research"
    runtime.write(plan_path, plan)
    calls = []
    def handler(request):
        calls.append((request.url.params["search"], request.url.params["cursor"]))
        if len(calls) == 1:
            return httpx.Response(200, json={"results": [work("W1")], "meta": {"count": 400, "next_cursor": "page2"}})
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "60"})
        if request.url.params["search"] == plan["queries"][0]["search"]:
            assert request.url.params["cursor"] == "page2"
            return httpx.Response(200, json={"results": [work("W2")], "meta": {"count": 400, "next_cursor": None}})
        return httpx.Response(200, json={"results": [work("W3")], "meta": {"count": 1, "next_cursor": None}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        limited = runtime.discover(workspace, plan_path, root=tmp_path, pages=3, client=client)
        assert len(calls) == 2  # No request from q2 after the q1 429.
        assert limited["complete"] is False and limited["status"] == "backoff"
        assert limited["pending_query_ids"] == ["q1", "q2"]
        assert limited["queries"]["q1"]["pages"] == 1
        assert limited["queries"]["q1"]["cursor"] == "page2"
        assert limited["queries"]["q2"]["pages"] == 0
        assert limited["backoff"]["retry_not_before_epoch"] == clock[0] + 60
        assert limited["keyword_requests"] == limited["usage"]["keyword_searches_today"] == 1
        assert [w["id"].rsplit("/", 1)[-1] for w in runtime.read(workspace / "works.json")] == ["W1"]
        candidate = json.loads((workspace / "candidates.jsonl").read_text())
        assert candidate["id"] == "W1" and candidate["abstract"] == "synthetic abstract"
        assert (workspace / "titles.tsv").read_text().startswith("W1\t")
        clock[0] += 59
        paused = runtime.discover(workspace, plan_path, root=tmp_path, pages=3, client=client)
        assert len(calls) == 2 and paused["complete"] is False
        assert paused["queries"]["q1"]["cursor"] == "page2"
        assert len(runtime.read(workspace / "openalex-requests.json")) == 2
        clock[0] += 1
        resumed = runtime.discover(workspace, plan_path, root=tmp_path, pages=3, client=client)
    assert len(calls) == 4 and resumed["complete"] is True
    assert resumed["pending_query_ids"] == [] and resumed["backoff"]["active"] is False
    assert resumed["queries"]["q1"]["pages"] == 2
    assert "failure_status" not in resumed["queries"]["q1"]
    assert resumed["keyword_requests"] == resumed["usage"]["keyword_searches_today"] == 3
    assert len(runtime.read(workspace / "works.json")) == 3
    assert [r["status"] for r in runtime.read(workspace / "openalex-requests.json")] == [200, 429, 200, 200]
    assert "synthetic-source-key" not in (workspace / "search-progress.json").read_text()
    assert "synthetic-source-key" not in (workspace / "openalex-requests.json").read_text()


def test_openalex_http_date_backoff_is_shared_with_new_workspace(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    retry_after = format_datetime(datetime.fromtimestamp(clock[0] + 120, timezone.utc), usegmt=True)
    plan_path = tmp_path / "input-plan.json"
    runtime.write(plan_path, rich_plan())
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"results": [work("W1")], "meta": {"count": 1, "next_cursor": None}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = runtime.discover(tmp_path / "first", plan_path, root=tmp_path, client=client)
        second = runtime.discover(tmp_path / "second", plan_path, root=tmp_path, client=client)
        assert len(calls) == 1
        assert first["backoff"] == second["backoff"]
        assert second["backoff"]["scope"] == "coverage_operator_shared"
        assert second["backoff"]["retry_not_before_epoch"] == clock[0] + 120
        assert second["backoff"]["retry_after"] == retry_after
        assert second["complete"] is False and second["pending_query_ids"] == ["q1"]
        assert runtime.read(tmp_path / "second" / "works.json") == []
        assert (tmp_path / "second" / "candidates.jsonl").read_text() == ""
        with sqlite3.connect(tmp_path / "out" / "coverage-openalex-usage.sqlite") as db:
            assert db.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 0
        clock[0] += 120
        resumed = runtime.discover(tmp_path / "second", plan_path, root=tmp_path, client=client)
    assert len(calls) == 2 and resumed["complete"] is True
    assert resumed["usage"]["keyword_searches_today"] == 1


def test_openalex_shared_backoff_keeps_latest_deadline(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    longer = runtime._openalex_backoff(tmp_path, deadline=clock[0] + 120, retry_after="120", source="retry_after")
    # An earlier in-flight response arriving later must not shorten the boundary.
    later = runtime._openalex_backoff(tmp_path, deadline=clock[0] + 30, retry_after="30", source="retry_after")
    assert later == longer and later["retry_not_before_epoch"] == clock[0] + 120


@pytest.mark.parametrize("retry_after", [None, "not a date", "-1", "1.5", "999999999999999999999999999999999999999"])
def test_openalex_429_without_valid_retry_header_still_stops_whole_net(tmp_path, monkeypatch, retry_after):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    plan = rich_plan()
    plan["queries"].append({"id": "q2", "search": "research annotation and rediscovery"})
    plan_path = tmp_path / "input-plan.json"
    runtime.write(plan_path, plan)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after} if retry_after is not None else {})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = runtime.discover(tmp_path / "research", plan_path, root=tmp_path, client=client)
        runtime.discover(tmp_path / "research", plan_path, root=tmp_path, client=client)
    assert len(calls) == 1 and result["complete"] is False
    assert result["pending_query_ids"] == ["q1", "q2"]
    assert result["backoff"]["retry_not_before_epoch"] == clock[0] + 60
    assert result["backoff"]["retry_after_source"] == "default_60_seconds"


def test_healthy_openalex_discovery_does_not_serialize_other_workspaces(tmp_path):
    plan_path = tmp_path / "input-plan.json"
    runtime.write(plan_path, rich_plan())
    simultaneous = threading.Barrier(2)
    errors = []
    results = []
    def handler(request):
        simultaneous.wait(timeout=3)
        return httpx.Response(200, json={"results": [work("W1")], "meta": {"count": 1, "next_cursor": None}})
    def discover(name):
        try:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                results.append(runtime.discover(tmp_path / name, plan_path, root=tmp_path, client=client))
        except Exception as error:
            errors.append(error)
    threads = [threading.Thread(target=discover, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == [] and len(results) == 2
    assert all(result["complete"] for result in results)
    with sqlite3.connect(tmp_path / "out" / "coverage-openalex-usage.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 2


def test_selected_batch_contains_failures_and_commits_catalog_incrementally(tmp_path):
    setup_sources(tmp_path)
    seen = []
    def fetch(source, transport):
        identifier = source["id"].rsplit("/", 1)[-1]
        seen.append(identifier)
        if identifier == "W2":
            assert [r["id"] for r in runtime.catalog(tmp_path)] == ["W1"]
            raise RuntimeError("Synthetic network failure")
        return (b"%PDF synthetic", "application/pdf", "https://example.org/W1.pdf"), False
    rows = runtime.acquire(tmp_path, ["W1", "W2"], workers=1, fetch=fetch, extract=extraction, transport=Transport())
    assert seen == ["W1", "W2"]  # W3 was never selected or downloaded.
    assert {r["status"] for r in rows} == {"source_retrieved", "retrieval_or_extraction_failed"}
    reference = runtime.catalog(tmp_path)[0]
    original = (tmp_path / reference["file"]).read_bytes()
    def should_not_fetch(*args):
        raise AssertionError("Committed source must not be fetched/overwritten")
    runtime.acquire(tmp_path, ["W1", "W2"], workers=2, fetch=should_not_fetch, extract=extraction, transport=Transport())
    assert (tmp_path / reference["file"]).read_bytes() == original
    passages = runtime.source(tmp_path, "W1", match="matched comparator", context=1)
    text = (tmp_path / reference["text_file"]).read_text()
    assert all(p["text"] == text[p["start"]:p["end"]] for p in passages)
    assert reference["full_text_status"] == "requires_source_verification"


def test_selected_downloads_really_run_concurrently(tmp_path):
    setup_sources(tmp_path)
    simultaneous = threading.Barrier(2)
    def fetch(source, transport):
        simultaneous.wait(timeout=2)
        return (b"%PDF synthetic", "application/pdf", "https://example.org/paper.pdf"), False
    rows = runtime.acquire(tmp_path, ["W1", "W2"], workers=2, fetch=fetch, extract=extraction, transport=Transport())
    assert all(r["status"] == "source_retrieved" for r in rows)


def test_binding_rejects_padding_foreign_spans_and_keeps_raw_research(tmp_path):
    setup_sources(tmp_path)
    def fetch(source, transport):
        return (b"%PDF " + source["id"].encode(), "application/pdf", "https://example.org/paper.pdf"), False
    runtime.acquire(tmp_path, ["W1", "W2"], fetch=fetch, extract=extraction, transport=Transport())
    w1 = runtime.source(tmp_path, "W1")[0]["id"]
    raw = {"executive_summary": [{"text": "Source reports a result", "claim_ids": ["C1"]}],
        "claims": [{"id": "C1", "text": "Attributed source result", "kind": "reported_finding",
            "uncertainty": {"class": "moderate", "score": 2, "rationale": "Indirect source"}, "evidence_ids": ["E1"], "study_result_ids": []}],
        "evidence": [{"id": "E1", "passage_ids": [w1], "interpretation": "Question-linked result", "relation": "supports"}],
        "study_results": [], "contradictions": [], "gaps": [], "residue": [], "next_questions": [], "caveats": [], "scope_note": "Synthetic fixture"}
    runtime.write(tmp_path / "investigation.json", raw)
    before = (tmp_path / "investigation.json").read_bytes()
    runtime.write(tmp_path / "run.json", {"minimum_papers": 2})
    runtime.write(tmp_path / "selection.json", {"selected_for_review_ids": ["W1", "W2", "W3"]})
    runtime.write(tmp_path / "reviewed-papers.json", [
        {"work_id": "W1", "eligibility": "core", "passage_ids_read": [w1], "investigated_sections": ["methods", "results"], "substantive_review_note": "Actual source-linked context", "related_study_ids": ["study1"]},
        {"work_id": "W2", "eligibility": "supporting", "passage_ids_read": [w1], "investigated_sections": ["results"], "substantive_review_note": "Incorrect foreign paper span"},
        {"work_id": "W3", "eligibility": "core", "passage_ids_read": [], "investigated_sections": [], "substantive_review_note": "Metadata only"}])
    payload = runtime.bind_payload(tmp_path)
    assert payload["accounting"]["substantively_reviewed_publications"] == 1
    assert payload["accounting"]["minimum_met"] is False
    assert any("another paper" in issue for issue in payload["mechanical_audit"]["issues"])
    assert payload["evidence"][0]["verbatims"][0]["id"] == w1
    assert (tmp_path / "investigation.json").read_bytes() == before


def test_native_usage_excludes_inherited_and_duplicate_receipts(tmp_path):
    rollout = tmp_path / "session.jsonl"
    db = sqlite3.connect(tmp_path / "state_1.sqlite")
    db.executescript("CREATE TABLE threads(id TEXT,rollout_path TEXT,model TEXT,reasoning_effort TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);")
    db.execute("INSERT INTO threads VALUES(?,?,?,?)", ("solo", str(rollout), "gpt-6.1-sol", "xhigh")); db.commit(); db.close()
    usage = {"input_tokens": 100, "cached_input_tokens": 80, "cache_write_input_tokens": None,
             "output_tokens": 30, "reasoning_output_tokens": 10, "total_tokens": 130}
    receipt = {"type": "token_usage_record", "payload": {"thread_id": "solo", "response_id": "response1", "usage": usage}}
    inherited = {"type": "token_usage_record", "payload": {"thread_id": "other", "response_id": "response2", "usage": usage}}
    rollout.write_text("\n".join(json.dumps(row) for row in (inherited, receipt, receipt)) + "\n")
    result = runtime.native_usage("solo", tmp_path)
    assert result["response_count"] == 1 and result["no_subagents_verified"] is True
    assert result["totals"]["uncached_input_tokens"] == 20
    assert result["totals"]["other_output_tokens"] == 20
    assert result["totals"]["cache_write_input_tokens"] is None


def test_scientific_invocation_keeps_authorized_models_and_approval_review(tmp_path):
    command = runtime.command_line("codex", protocol.RESEARCH_MODEL, protocol.RESEARCH_EFFORT, tmp_path, tmp_path / "schema.json", tmp_path / "response.json")
    assert command[command.index("-m") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="xhigh"' in command
    assert "--approve-for-me" in command and "agents.enabled=false" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert protocol.AUDIT_MODEL == "gpt-6-astra" and protocol.AUDIT_EFFORT == "ultra"


def test_successful_upstream_search_count_survives_bad_metadata(tmp_path):
    import pytest
    plan_path = tmp_path / "input-plan.json"
    runtime.write(plan_path, rich_plan())
    def handler(request):
        return httpx.Response(200, json={"results": [{"display_name": "Malformed work"}], "meta": {"next_cursor": None}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(KeyError):
            runtime.discover(tmp_path / "research", plan_path, root=tmp_path, pages=1, client=client)
    progress = runtime.read(tmp_path / "research" / "search-progress.json")
    assert progress["keyword_requests"] == 1
    assert progress["usage"]["keyword_searches_today"] == 1
    assert runtime.read(tmp_path / "research" / "openalex-requests.json")[0]["status"] == 200


def test_subscription_preview_is_labeled_and_never_counted_as_full_review(tmp_path):
    setup_sources(tmp_path, ("W1",))
    def fetch(work, transport):
        return (b"%PDF synthetic", "application/pdf", "https://example.org/preview.pdf"), False
    def preview(raw, kind, work):
        return ("This is a preview of subscription content.\n\n" + "Synthetic context. " * 50, 1, [], "pdf")
    runtime.acquire(tmp_path, ["W1"], fetch=fetch, extract=preview, transport=Transport())
    reference = runtime.catalog(tmp_path)[0]
    assert reference["preview_marker_detected"] is True
    assert reference["full_text_status"] == "preview_suspected"
    pid = runtime.source(tmp_path, "W1")[0]["id"]
    runtime.write(tmp_path / "investigation.json", {"claims": [], "evidence": [], "study_results": []})
    runtime.write(tmp_path / "selection.json", {"selected_for_review_ids": ["W1"]})
    runtime.write(tmp_path / "reviewed-papers.json", [{"work_id": "W1", "eligibility": "core", "passage_ids_read": [pid],
        "substantive_review_note": "A preview was incorrectly marked core", "investigated_sections": ["methods"]}])
    assert runtime.bind_payload(tmp_path)["accounting"]["substantively_reviewed_publications"] == 0


def test_progress_refresh_exports_committed_claims_and_exact_role_usage(tmp_path, monkeypatch):
    runtime.write(tmp_path / "run.json", {"question": "Synthetic?", "status": "researching"})
    runtime.write(tmp_path / "investigation.json", {"claims": [{"id": "C1", "text": "Committed finding", "evidence_ids": []}]})
    usage = {"available": True, "totals": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10}, "receipts": []}
    monkeypatch.setattr(runtime, "native_usage", lambda thread: usage)
    result = runtime.refresh_committed_progress(tmp_path, "research", "gpt-6.1-sol", "xhigh", "solo", "2026-10-14T00:00:00Z", 12)
    assert result["answer"]["claims"][0]["text"] == "Committed finding"
    assert result["run"]["status"] == "in_progress" and result["run"]["runtime_status"] == "researching"
    profile = runtime.read(tmp_path / "profile.json")
    assert profile["active_role"] == "research"
    assert profile["roles"]["research"]["ended_at"] is None
    assert profile["roles"]["research"]["tokens"] == usage["totals"]
    runtime.write(tmp_path / "research-receipt.json", {"ended_at": "2026-10-14T00:01:00Z", "seconds": 60, "usage": usage})
    runtime.refresh_committed_progress(tmp_path, "audit", "gpt-6-astra", "ultra", "audit-solo", "2026-10-14T00:01:00Z", 2)
    refreshed = runtime.read(tmp_path / "profile.json")
    assert refreshed["roles"]["research"]["active"] is False
    assert refreshed["roles"]["research"]["ended_at"] == "2026-10-14T00:01:00Z"
    assert refreshed["active_role"] == "audit"
    from canary import coverage_export
    monkeypatch.setattr(coverage_export, "export_bundle", lambda workspace: (_ for _ in ()).throw(ValueError("Malformed intermediate artifact")))
    assert runtime.refresh_committed_progress(tmp_path, "research", "gpt-6.1-sol", "xhigh", "solo", "2026-10-14T00:00:00Z", 22) is None
    assert "progress_export_failed" in (tmp_path / "runtime-progress.jsonl").read_text()


@pytest.mark.parametrize("preexisting_empty", [False, True])
def test_new_run_reserves_fresh_or_preexisting_empty_workspace(tmp_path, monkeypatch, preexisting_empty):
    workspace = tmp_path / "research"
    if preexisting_empty:
        workspace.mkdir()
    calls = []
    def stop_without_model(*args, **kwargs):
        calls.append(args[1])
        raise RuntimeError("Offline session boundary")
    monkeypatch.setattr(runtime, "scientific_session", stop_without_model)
    with pytest.raises(RuntimeError, match="Offline session boundary"):
        runtime.run("Fresh question?", workspace, executable="offline-codex")
    assert calls == ["research"]
    assert (workspace / ".run-reservation").is_file()
    assert runtime.read(workspace / "run.json")["question"] == "Fresh question?"
    before = {p.name: p.read_bytes() for p in workspace.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="empty workspace"):
        runtime.run("Replacement question?", workspace, executable="offline-codex")
    assert calls == ["research"]
    assert {p.name: p.read_bytes() for p in workspace.iterdir() if p.is_file()} == before


def test_run_rejects_occupied_workspace_without_adding_or_overwriting_files(tmp_path, monkeypatch):
    workspace = tmp_path / "research"
    workspace.mkdir()
    sentinel = workspace / "retained-source.txt"
    sentinel.write_bytes(b"Existing source remains unchanged")
    def never_run(*args, **kwargs):
        raise AssertionError("Occupied workspace must not start a scientific session")
    monkeypatch.setattr(runtime, "scientific_session", never_run)
    with pytest.raises(ValueError, match="empty workspace"):
        runtime.run("New question?", workspace, executable="offline-codex")
    assert {p.name: p.read_bytes() for p in workspace.iterdir()} == {sentinel.name: b"Existing source remains unchanged"}


def test_concurrent_process_runs_have_one_atomic_workspace_owner(tmp_path, monkeypatch):
    # Runtime already requires POSIX flock. Fork preserves offline stubs without
    # importing/executing a real scientific harness in either child process.
    context = multiprocessing.get_context("fork")
    workspace = (tmp_path / "research").resolve()
    workspace.mkdir()
    both_saw_empty = context.Barrier(2)
    entered = context.Event()
    rejected = context.Event()
    release = context.Event()
    messages = context.Queue()
    original_iterdir = Path.iterdir
    def synchronized_empty_check(path):
        if path == workspace:
            snapshot = list(original_iterdir(path))
            both_saw_empty.wait(timeout=5)
            return iter(snapshot)
        return original_iterdir(path)
    def stop_without_model(*args, **kwargs):
        messages.put(("entered", runtime.read(workspace / "run.json")["question"]))
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("Offline session release timed out")
        raise RuntimeError("Offline session boundary")
    monkeypatch.setattr(Path, "iterdir", synchronized_empty_check)
    monkeypatch.setattr(runtime, "scientific_session", stop_without_model)
    def attempt(question):
        try:
            runtime.run(question, workspace, executable="offline-codex")
        except Exception as error:
            messages.put(("result", question, type(error).__name__, str(error)))
            if isinstance(error, ValueError):
                rejected.set()
    processes = [context.Process(target=attempt, args=(question,)) for question in ("Question A?", "Question B?")]
    for process in processes:
        process.start()
    try:
        assert entered.wait(timeout=5), "One caller must reach the offline session"
        assert rejected.wait(timeout=5), "The competing caller must lose reservation before writing assets"
        owner = runtime.read(workspace / "run.json")["question"]
        protected = {name: (workspace / name).read_bytes() for name in (
            "question-plan-schema.json", "investigation-schema.json", "AGENTS.md", ".run-reservation")}
    finally:
        release.set()
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)
    records = [messages.get(timeout=2) for _ in range(3)]
    assert [r[1] for r in records if r[0] == "entered"] == [owner]
    outcomes = {r[1]: r[2:] for r in records if r[0] == "result"}
    assert len(outcomes) == 2
    assert outcomes[owner] == ("RuntimeError", "Offline session boundary")
    loser = next(question for question in outcomes if question != owner)
    assert outcomes[loser][0] == "ValueError" and "reserved" in outcomes[loser][1]
    assert runtime.read(workspace / "run.json")["question"] == owner
    assert runtime.read(workspace / "run.json")["status"] == "failed"
    assert {name: (workspace / name).read_bytes() for name in protected} == protected
    messages.close()
    messages.join_thread()


@pytest.mark.parametrize("complete", [False, True])
def test_rejected_query_change_keeps_committed_plan_candidates_and_exported_net(tmp_path, complete):
    from canary.coverage_export import export_bundle
    from test_coverage_evidence_ui import run_dashboard
    workspace = tmp_path / "research"
    import re
    prompt = protocol.research_prompt("Synthetic question?", "canary", 50)
    draft = re.search(r"1\. Write ([\w.-]+)", prompt)[1]
    command_plan = re.search(r"discover --workspace \. --plan ([\w.-]+)", prompt)[1]
    assert draft == command_plan and draft != "question-plan.json"
    assert "never overwrite that canonical file directly" in prompt
    assert "assign a new ID whenever changing a search string" in prompt
    plan_path = workspace / draft
    original = rich_plan(); runtime.write(plan_path, original)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [work("W1")], "meta": {"count": 200, "next_cursor": None if complete else "page2"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        runtime.discover(workspace, plan_path, root=tmp_path, pages=1, client=client)
        saved = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file() and p != plan_path}
        changed = rich_plan()
        changed["queries"][0]["search"] = "silently changed query definition"
        changed["queries"].insert(0, {"id": "qnew", "search": "new branch before invalid reused ID"})
        runtime.write(plan_path, changed)
        with pytest.raises(ValueError, match="Change a query ID"):
            runtime.discover(workspace, plan_path, root=tmp_path, pages=1, client=client)
    assert len(calls) == 1, "Rejected query changes consume no further upstream requests"
    assert {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file() and p != plan_path} == saved
    result = export_bundle(workspace)
    net = runtime.read(workspace / "query-net.json")
    assert net == original
    assert result["coverage"]["search_progress"]["queries"]["q1"]["search"] == original["queries"][0]["search"]
    assert runtime.read(workspace / "works.json")[0]["query_ids"] == ["q1"]
    data = {"query_net": net, "answer": result["answer"], "coverage": result["coverage"], "run": result["run"]}
    run_dashboard(tmp_path, data, r"""
const before=JSON.stringify(graph),html=queryNetHtml(graph);
assert.ok(html.includes(esc(graph.query_net.queries[0].search)));
assert.match(html,/q1 · 1 result page recorded/);
assert.match(html,new RegExp(graph.coverage.search_progress.queries.q1.complete ? ' · complete' : ' · incomplete'));
assert.doesNotMatch(html,/silently changed query definition|qnew/);
assert.equal(JSON.stringify(graph),before);
""")


def test_new_query_id_can_extend_a_committed_search_net(tmp_path):
    plan_path = tmp_path / "input-plan.json"; workspace = tmp_path / "research"
    runtime.write(plan_path, rich_plan()); calls = []
    def handler(request):
        calls.append(request)
        ident = "W1" if request.url.params["search"] == rich_plan()["queries"][0]["search"] else "W2"
        return httpx.Response(200, json={"results": [work(ident)], "meta": {"next_cursor": None}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        runtime.discover(workspace, plan_path, root=tmp_path, pages=1, client=client)
        extended = rich_plan(); extended["queries"].append({"id": "q2", "search": "new complementary query"})
        runtime.write(plan_path, extended)
        progress = runtime.discover(workspace, plan_path, root=tmp_path, pages=1, client=client)
    assert len(calls) == 2 and calls[1].url.params["search"] == "new complementary query"
    assert runtime.read(workspace / "question-plan.json") == extended
    assert progress["queries"]["q1"]["pages"] == progress["queries"]["q2"]["pages"] == 1
    assert progress["complete"] is True
    assert {w["id"].split("/")[-1] for w in runtime.read(workspace / "works.json")} == {"W1", "W2"}
