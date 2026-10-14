"""Offline runtime contracts; fake acquisition/receipts are not scientific validation."""
import json
import sqlite3
import threading

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
