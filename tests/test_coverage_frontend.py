"""Deterministic production entrypoint, source-context and operational receipts."""
import json
from concurrent.futures import ThreadPoolExecutor
import threading
import sqlite3
from pathlib import Path

import pytest

from canary import cli, coverage_runtime, coverage_export, dashboard, runs


def test_ask_routes_to_coverage_without_touching_legacy_review(tmp_path, monkeypatch):
    calls = []
    def research(question, workspace, **options):
        reserved = options.pop("on_reserved")
        calls.append((question, workspace, options))
        Path(workspace).mkdir()
        reserved()
        (Path(workspace) / "run.json").write_text('{}')
    monkeypatch.setattr(coverage_runtime, "run", research)
    monkeypatch.setattr(coverage_export, "export_bundle", lambda directory: calls.append(directory))
    monkeypatch.setattr(cli, "review", lambda *a, **k: pytest.fail("legacy review invoked"))
    workspace = tmp_path / "new"
    assert cli.engine_main(["ask", "Question?", "--out", str(workspace)]) == 0
    assert calls[0] == ("Question?", str(workspace), {"minimum_papers": 50, "audit_seconds": 600})
    assert calls[1] == str(workspace)


def test_fixed_question_launcher_retains_question_as_one_argument(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "QUESTION_ENGINE_ROOT", tmp_path)
    launcher = tmp_path / "scripts" / "coverage_run.sh"
    argv = ["bash", str(launcher), "ask", "Question with $(literal) syntax?"]
    assert dashboard.validate_launch(argv) == argv
    for invalid in (argv + ["--extra"], ["sh", *argv[1:]], ["bash", str(launcher), "ask", ""]):
        with pytest.raises(ValueError):
            dashboard.validate_launch(invalid)


def test_operator_keyword_counter_is_read_only_and_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "ROOT", tmp_path)
    path = tmp_path / "out" / "coverage-openalex-usage.sqlite"
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE searches(at TEXT NOT NULL)")
        db.executemany("INSERT INTO searches VALUES(?)", [(runs.utcnow(),), (runs.utcnow(),), ("2000-01-01T00:00:00Z",)])
    result = dashboard.openalex_status()
    assert result["keyword_searches_today"] == 2
    assert "gateway and other clients excluded" in result["count_scope"]
    assert result["discovery"]["status"] == "available"
    assert result["discovery"]["probe_request_made"] is False
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 3


def test_dashboard_renders_discovery_readiness(tmp_path):
    import re, subprocess
    script = dashboard.UI_PATH.read_text()
    functions = []
    for name in ("discoveryHtml", "usageHtml"):
        match = re.search(r"(?:^|\n)\s*function " + name + r"\([^\n]*", script)
        assert match is not None
        end = re.search(r"\n\s*function ", script[match.end():])
        functions.append(script[match.start():match.end() + end.start()] if end else script[match.start():])
    probe = tmp_path / "discovery-readiness.js"
    probe.write_text("\n".join(functions) + """
const assert=require('node:assert/strict');
const esc=String;
const base={keyword_searches_today:3,date_utc:'2026-10-30',full_day_observed:false,count_scope:'scope'};
const paused=usageHtml({...base,discovery:{status:'paused',openalex_allowance:'configured_key',
  backoff:{retry_not_before:'2026-10-30T12:00:00+00:00',reason:'rate_limited'}}});
assert.match(paused,/Discovery paused for configured key until 2026-10-30T12:00:00/);
const available=usageHtml({...base,discovery:{status:'available',openalex_allowance:'anonymous',backoff:null}});
assert.match(available,/No recorded discovery pause for anonymous/);
const unknown=usageHtml({discovery:{status:'unknown',unknown_reason:'recorded_backoff_unreadable',backoff:null}});
assert.match(unknown,/Discovery readiness unknown/);
""")
    result = subprocess.run(["node", str(probe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_coverage_run_api_keeps_audit_unknown_separate_from_answer(tmp_path):
    run = {"kind": "question", "execution_mode": "coverage-guided full-paper research",
           "selection_method": "coverage-guided", "question": "Question?", "status": "answered",
           "papers_reviewed": 1, "papers_core": 1, "papers_supporting": 0, "target_papers": 50,
           "audit_status": "partial", "generated_at": "2026-10-14T00:00:00Z"}
    (tmp_path / "run.json").write_text(json.dumps(run))
    (tmp_path / "answer.json").write_text(json.dumps({"claims": [{"id": "C1", "text": "Attributed finding."}],
        "source_checks": [{"claim_id": "C1", "assessment": "not_checked"}], "references": []}))
    (tmp_path / "coverage.json").write_text(json.dumps({"reviewed_papers": 1, "target_papers": 50}))
    result = dashboard.research_bundle(tmp_path)
    assert result["run"]["audit_status"] == "partial"
    assert result["answer"]["source_checks"][0]["assessment"] == "not_checked"
    assert result["coverage"]["target_papers"] == 50


def test_ask_refusal_keeps_existing_bundle_byte_identical(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    (old / "run.json").write_text('{"kind":"cycle","status":"converged"}')
    (old / "synthesis.md").write_text("Existing work")
    before = {p.name: p.read_bytes() for p in old.iterdir()}
    monkeypatch.setattr(coverage_runtime, "run", lambda *a, **k: pytest.fail("Existing workspace admitted"))
    monkeypatch.setattr(coverage_export, "export_bundle", lambda *a, **k: pytest.fail("Existing bundle exported"))
    assert cli.engine_main(["ask", "New question?", "--out", str(old)]) == 1
    assert {p.name: p.read_bytes() for p in old.iterdir()} == before


def test_export_reports_real_search_count_and_saves_partial_answer(tmp_path):
    run = {"kind": "question", "question": "Synthetic question?", "status": "partial", "answered": True}
    (tmp_path / "run.json").write_text(json.dumps(run))
    claim = {"id": "C1", "text": "Source reports a synthetic result.", "uncertainty": {"class": "high", "score": 3, "rationale": "Fixture only."}, "evidence_ids": []}
    (tmp_path / "investigation.json").write_text(json.dumps({"claims": [claim]}))
    (tmp_path / "openalex-requests.json").write_text(json.dumps([{"status": 200}, {"status": 429}, {"status": 200}]))
    result = coverage_export.export_bundle(tmp_path)
    assert result["run"]["keyword_requests"] == 2
    assert result["run"]["status"] == "partial"
    report = (tmp_path / "answer.md").read_text()
    assert claim["text"] in report and "Uncertainty: high (3/3; ordinal)" in report
    assert "Audit: not_checked" in report
    assert dashboard.research_bundle(tmp_path)["synthesis_md"] == report
    assert runs._has_answer(tmp_path, result["run"], "partial") is True


def test_nested_export_refreshes_canonical_efficiency_and_access_receipts(tmp_path):
    source = tmp_path / "scientist"
    source.mkdir()
    (source / "download-receipts").mkdir()
    (source / "download-receipts" / "W9.json").write_text(json.dumps({"work_id": "W9", "status": "unavailable"}))
    (source / "openalex-requests.json").write_text(json.dumps([{"status": 200}, {"status": 200}]))
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "researching"}))
    (source / "investigation.json").write_text(json.dumps({"claims": []}))
    first = {"scope_note": "First committed version", "comparisons": []}
    (source / "efficiency.json").write_text(json.dumps(first))
    initial = coverage_export.export_bundle(tmp_path, source_dir=source, research_dir=source)
    assert initial["answer"]["source_efficiency"] == first
    second = {"scope_note": "Second committed version", "comparisons": [], "remaining_unknown": ["Denominator absent"]}
    (source / "efficiency.json").write_text(json.dumps(second))
    updated = coverage_export.export_bundle(tmp_path, source_dir=source, research_dir=source)
    assert updated["answer"]["source_efficiency"] == second
    assert json.loads((tmp_path / "source-efficiency.json").read_text()) == second
    assert updated["access"]["excluded"] == [{"work_id": "W9", "status": "unavailable"}]
    assert updated["run"]["keyword_requests"] == 2


def test_retrieved_preview_counts_and_log_are_distinct_from_reviewed(tmp_path):
    texts = tmp_path / "texts";texts.mkdir()
    body = "This is a preview of subscription content."
    (texts / "W1.txt").write_text(body)
    ref = {"id": "W1", "text_file": "texts/W1.txt", "file": "papers/W1.pdf", "full_text_status": "requires_source_verification"}
    (tmp_path / "references.json").write_text(json.dumps([ref]))
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "researching"}))
    result = coverage_export.export_bundle(tmp_path)
    assert result["coverage"]["retrieved_sources"] == 1
    assert result["coverage"]["preview_suspected_sources"] == 1
    assert result["coverage"]["available_papers"] == 0
    metrics = runs._bundle_log_metrics(tmp_path, result["run"], {}, [], {"status": "in_progress"})
    assert metrics["papers_acquired"] == 1 and metrics["papers_covered"] == 0
    assert metrics["preview_suspected_sources"] == 1


def test_real_dashboard_renders_committed_claims_without_an_executive_summary(tmp_path):
    import re, subprocess
    script = dashboard.UI_PATH.read_text()
    names = ("qaGraph", "active", "nativeErrorHtml", "answerHtml")
    functions = []
    for name in names:
        match = re.search(r"(?:^|\n)\s*function " + name + r"\([^\n]*", script)
        assert match is not None
        end = re.search(r"\n\s*function ", script[match.end():])
        functions.append(script[match.start():match.end() + end.start()] if end else script[match.start():])
    probe = tmp_path / "partial-answer.js"
    probe.write_text("\n".join(functions) + """
const assert=require('node:assert/strict');
const esc=String, cliResearch=()=>true, acquisitionHtml=()=>'', coverageAuditHtml=()=>'<p>Audit pending</p>',
      sourceEfficiencyHtml=()=>'<p>Recorded denominator limits</p>', peerReviewHtml=()=>'',
      researchStructureHtml=()=>'', findingHtml=(g,c)=>'<p>'+c.text+'</p>';
const data={run:{status:'researching'},answer:{claims:[{id:'C1',text:'Committed attributed finding'}],executive_summary:[],gaps:[],caveats:[],next_questions:[]}};
assert.equal(qaGraph(data),data.answer);
assert.equal(qaGraph({answer:{claims:[],executive_summary:[]}}),null);
assert.equal(active({kind:'question',status:'researching'}),true);
assert.equal(active({kind:'question',status:'auditing'}),true);
const html=answerHtml({kind:'question',status:'researching'},data);
assert.match(html,/Committed attributed finding/);
assert.match(html,/Recorded denominator limits/);
assert.match(html,/Audit pending/);
assert.doesNotMatch(html,/aria-label="Executive summary"/);
""")
    result = subprocess.run(["node", str(probe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_export_count_matches_binding_for_same_body_and_missing_sections(tmp_path):
    import hashlib
    from canary import coverage_runtime as runtime
    refs, reviews = [], []
    for number in (1, 2, 3):
        wid = "W" + str(number)
        text = "Quoted result for " + wid
        raw = b"same paper bytes" if number < 3 else b"different bytes"
        for name in ("texts", "papers", "passages"):
            (tmp_path / name).mkdir(exist_ok=True)
        (tmp_path / "texts" / (wid + ".txt")).write_text(text)
        (tmp_path / "papers" / (wid + ".pdf")).write_bytes(raw)
        pid = wid + "-p0001"
        span = {"id": pid, "reference_id": wid, "start": 0, "end": len(text), "text": text, "page": 1}
        (tmp_path / "passages" / (wid + ".jsonl")).write_text(json.dumps(span) + "\n")
        refs.append({"id": wid, "doi": "https://doi.org/10.9999/" + wid, "file": "papers/" + wid + ".pdf", "text_file": "texts/" + wid + ".txt",
                     "body_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
        reviews.append({"work_id": wid, "eligibility": "core", "passage_ids_read": [pid], "substantive_review_note": "Synthetic review",
                        "investigated_sections": ["results"] if number < 3 else []})
    runtime.write(tmp_path / "references.json", refs)
    for reference in refs:
        runtime.write(tmp_path / "download-receipts" / (reference["id"] + ".json"),
                      {"work_id": reference["id"], "status": "source_retrieved", "reference": reference})
    runtime.write(tmp_path / "reviewed-papers.json", reviews)
    runtime.write(tmp_path / "selection.json", {"selected_for_review_ids": ["W1", "W2", "W3"]})
    runtime.write(tmp_path / "run.json", {"question": "Synthetic?", "minimum_papers": 50})
    runtime.write(tmp_path / "investigation.json", {"claims": [], "evidence": [], "study_results": []})
    runtime.write(tmp_path / "question-plan.json", {"question": "Synthetic?"})
    bound = runtime.bind_payload(tmp_path)
    exported = coverage_export.export_bundle(tmp_path)
    assert bound["accounting"]["substantively_reviewed_publications"] == exported["coverage"]["reviewed_papers"] == 1
    assert bound["accounting"]["core_publications"] == exported["coverage"]["core_papers"] == 1


def test_failed_session_receipt_remains_in_exported_usage(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "failed"}))
    receipt = {"ended_at": "2026-10-14T00:01:00Z", "thread_id": "solo", "usage": {"totals": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10, "total_tokens": 110}}}
    (tmp_path / "research-receipt.json").write_text(json.dumps(receipt))
    result = coverage_export.export_bundle(tmp_path)
    assert result["run"]["status"] == "failed"
    assert runs.parse_bundle(tmp_path)["input_tokens"] == 100
    assert runs.parse_bundle(tmp_path)["output_tokens"] == 10
    assert json.loads((tmp_path / "profile.json").read_text())["roles"]["research"]["active"] is False


def test_unknown_native_total_is_not_reported_as_partial_or_zero(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"kind": "question", "receipts": [{"usage": {"total_tokens": 10}}, {"usage": {"total_tokens": None}}]}))
    assert runs.parse_bundle(tmp_path)["usage"]["tokens"] is None
    (tmp_path / "run.json").write_text(json.dumps({"kind": "question", "receipts": []}))
    assert runs.parse_bundle(tmp_path)["usage"]["tokens"] is None


def test_native_failure_and_restart_completion_remain_visible(tmp_path):
    import io
    bundle = tmp_path / "bundle";bundle.mkdir()
    registry = tmp_path / "registry.json"
    run = {"kind": "question", "execution_mode": "coverage-guided full-paper research", "status": "researching", "generated_at": "2026-10-14T00:00:00Z"}
    (bundle / "run.json").write_text(json.dumps(run))
    runs.register_start("native", "Native", "", "question", str(bundle), path=registry)
    class Failed:
        def wait(self):return 1
    supervisor = dashboard.Supervisor(registry=registry, root=tmp_path)
    supervisor._wait("native", Failed(), io.StringIO())
    failure = supervisor.reconcile()[0]
    assert failure["status"] == "failed" and failure["ended_at"] is not None
    assert runs.log_rows(registry, tmp_path)[0]["status"] == "failed"
    runs.register_start("native", "Native", "", "question", str(bundle), path=registry)
    run.update(status="answered", ended_at="2026-10-14T00:02:00Z")
    (bundle / "run.json").write_text(json.dumps(run))
    assert runs.log_rows(registry, tmp_path)[0]["status"] == "answered"


def test_failed_coverage_log_preserves_answer_and_unknown_role_totals(tmp_path):
    bundle = tmp_path / "bundle";bundle.mkdir()
    registry = tmp_path / "registry.json"
    (bundle / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "failed"}))
    (bundle / "investigation.json").write_text(json.dumps({"claims": [{"id": "C1", "text": "Preserved attributed finding", "evidence_ids": []}]}))
    (bundle / "profile.json").write_text(json.dumps({"research": {"usage": {"totals": {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 80}}},
        "audit": {"usage": {"available": False, "reason": "Receipt unavailable"}}}))
    coverage_export.export_bundle(bundle)
    runs.upsert({"key": "failed", "name": "Failed coverage", "kind": "question", "status": "failed", "bundle": str(bundle)}, registry)
    row = runs.log_rows(registry, tmp_path)[0]
    assert row["answered"] is True
    assert row["input_tokens"] is None and row["output_tokens"] is None


def test_below_target_export_preserves_reported_uncertified_scarcity(tmp_path):
    statement = {"reason": "Synthetic scope contains fewer accessible comparisons.",
                 "searches_attempted": ["Synthetic query"], "remaining_unknown": ["Unindexed records"]}
    claim = {"id": "C1", "text": "Source reports a synthetic result.", "evidence_ids": []}
    payload = {"accounting": {"scarcity_statement": statement, "scarcity_independently_certified": False},
               "research": {"claims": [claim]}}
    (tmp_path / "payload.json").write_text(json.dumps(payload))
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "complete", "minimum_papers": 50}))
    result = coverage_export.export_bundle(tmp_path)
    assert result["run"]["status"] == "answered"
    assert result["coverage"]["reviewed_papers"] < result["coverage"]["target_papers"]
    assert result["coverage"]["scarcity_statement"] == statement
    assert result["coverage"]["scarcity_independently_certified"] is False
    assert dashboard.research_bundle(tmp_path)["coverage"]["scarcity_statement"] == statement
    assert json.loads((tmp_path / "payload.json").read_text()) == payload


@pytest.mark.parametrize("roles,expected", [
    ({}, None),
    ({"research": {"usage": {}}}, None),
    ({"research": {"usage": {"response_count": 5}}, "audit": {"usage": {}}}, None),
    ({"research": {"usage": {"response_count": 5}}, "audit": {"usage": {"response_count": None}}}, None),
    ({"research": {"usage": {"response_count": 5}}, "audit": {"usage": {"available": False, "response_count": 0}}}, None),
    ({"research": {"usage": {"available": False, "response_count": 0}}}, None),
    ({"research": {"usage": {"available": True, "response_count": 0}}}, 0),
    ({"research": {"usage": {"response_count": 0}}}, 0),
    ({"research": {"usage": {"response_count": 5}}, "audit": {"usage": {"response_count": 3}}}, 8),
    ({"research": {"responses": 5}, "audit": {"responses": 0}}, 5),
])
def test_native_model_calls_are_exact_only_when_every_role_is_known(tmp_path, roles, expected):
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "researching"}))
    (tmp_path / "profile.json").write_text(json.dumps({"roles": roles}))
    result = coverage_export.export_bundle(tmp_path)
    assert result["run"]["llm_calls"] == expected
    assert runs.parse_bundle(tmp_path)["usage"]["model_calls"] == expected
    assert dashboard.research_bundle(tmp_path)["usage"]["model_calls"] == expected


def test_native_run_without_response_count_keeps_model_calls_unknown(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"kind": "question", "status": "researching"}))
    assert runs.parse_bundle(tmp_path)["usage"]["model_calls"] is None


def test_unknown_claim_evidence_is_preserved_and_exposed_as_linked_binding_issue(tmp_path):
    claim = {"id": "C1", "text": "The source reports a synthetic finding.", "evidence_ids": ["E_missing"],
             "uncertainty": {"class": "high", "score": 3, "rationale": "Synthetic only."}}
    investigation = {"claims": [claim], "evidence": []}
    (tmp_path / "investigation.json").write_text(json.dumps(investigation))
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "complete"}))
    result = coverage_export.export_bundle(tmp_path)
    exported = result["answer"]["claims"][0]
    assert exported["text"] == claim["text"] and exported["uncertainty"] == claim["uncertainty"]
    assert exported["evidence_ids"] == claim["evidence_ids"]
    assert result["answer"]["binding_issues"] == [{"kind": "missing_evidence", "claim_id": "C1", "evidence_id": "E_missing"}]
    assert dashboard.research_bundle(tmp_path)["answer"]["binding_issues"] == result["answer"]["binding_issues"]
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    report = (tmp_path / "answer.md").read_text()
    assert "Provenance failure: missing evidence E_missing" in report
    assert "Evidence E_missing · supports" not in report


@pytest.mark.parametrize("status,expected", [("researching", "in_progress"), ("failed", "failed"), ("complete", "complete"), ("answered", "complete")])
def test_empty_native_export_removes_stale_stub_without_hiding_sources(tmp_path, status, expected):
    stale = "# Synthetic?\n\n## References\nR1 · Saved source\n"
    quote = "A saved synthetic source passage."
    ref = {"id": "R1", "title": "Saved source", "full_text_status": "requires_source_verification",
           "file": "papers/R1.pdf", "text_file": "texts/R1.txt", "text_chars": len(quote)}
    for name in ("papers", "texts", "passages"):
        (tmp_path / name).mkdir()
    (tmp_path / "papers/R1.pdf").write_bytes(b"synthetic saved body")
    (tmp_path / "texts/R1.txt").write_text(quote)
    span = {"id": "R1-p1", "reference_id": "R1", "text": quote, "start": 0, "end": len(quote)}
    (tmp_path / "passages/R1.jsonl").write_text(json.dumps(span) + "\n")
    (tmp_path / "selection.json").write_text(json.dumps({"selected_for_review_ids": ["R1"]}))
    (tmp_path / "reviewed-papers.json").write_text(json.dumps([{"work_id": "R1", "eligibility": "supporting",
        "passage_ids_read": ["R1-p1"], "investigated_sections": ["results"], "substantive_review_note": "Synthetic context review."}]))
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": status}))
    (tmp_path / "investigation.json").write_text(json.dumps({"claims": [], "executive_summary": [" ", {"text": "\n"}]}))
    (tmp_path / "references.json").write_text(json.dumps([ref]))
    (tmp_path / "answer.md").write_text(stale)
    result = coverage_export.export_bundle(tmp_path)
    assert result["run"]["status"] == expected and result["run"]["answered"] is False
    assert not (tmp_path / "answer.md").exists()
    view = dashboard.research_bundle(tmp_path)
    assert view["synthesis_md"] is None and view["answer"]["references"][0]["id"] == "R1"
    assert runs._has_answer(tmp_path, result["run"], expected) is False
    summary = dashboard.research_summary({"bundle": str(tmp_path)})
    assert summary["claims_count"] == 0 and summary["has_synthesis"] is False


@pytest.mark.parametrize("summary", [["Reported synthetic summary."], [{"text": "Reported synthetic summary.", "claim_ids": []}]])
def test_native_summary_content_remains_a_downloadable_report(tmp_path, summary):
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "complete"}))
    (tmp_path / "investigation.json").write_text(json.dumps({"claims": [], "executive_summary": summary}))
    result = coverage_export.export_bundle(tmp_path)
    assert result["run"]["status"] == "answered" and result["run"]["answered"] is True
    assert "Reported synthetic summary." in (tmp_path / "answer.md").read_text()
    assert runs._has_answer(tmp_path, result["run"], "answered") is True
    assert dashboard.research_summary({"bundle": str(tmp_path)})["has_synthesis"] is True


def test_duplicate_claim_ids_preserve_each_occurrence_without_assigning_id_only_audit(tmp_path):
    first, second = "First exact synthetic source result.", "Second different synthetic source result."
    text = first + "\n" + second
    for name in ("papers", "texts", "passages"):
        (tmp_path / name).mkdir()
    (tmp_path / "papers/R1.pdf").write_bytes(b"Synthetic complete saved body")
    (tmp_path / "texts/R1.txt").write_text(text)
    spans = [{"id": "p1", "reference_id": "R1", "text": first, "start": 0, "end": len(first)},
             {"id": "p2", "reference_id": "R1", "text": second, "start": len(first) + 1, "end": len(text)}]
    (tmp_path / "passages/R1.jsonl").write_text("".join(json.dumps(p) + "\n" for p in spans))
    ref = {"id": "R1", "title": "Synthetic source", "file": "papers/R1.pdf", "text_file": "texts/R1.txt"}
    (tmp_path / "references.json").write_text(json.dumps([ref]))
    claims = [{"id": "C1", "text": "First recorded claim.", "evidence_ids": ["E1"]},
              {"id": "C1", "text": "Second distinct recorded claim.", "evidence_ids": ["E2"]},
              {"id": "C2", "text": "Unique recorded claim.", "evidence_ids": ["E1"]}]
    investigation = {"claims": claims, "evidence": [{"id": "E1", "passage_ids": ["p1"], "relation": "supports"},
                                                     {"id": "E2", "passage_ids": ["p2"], "relation": "qualifies"}]}
    audit = {"audit_complete": True, "claim_checks": [{"claim_id": "C1", "assessment": "supported", "reason": "Ambiguous ID-only audit."},
                                                     {"claim_id": "C2", "assessment": "supported", "reason": "Unique source check."}]}
    (tmp_path / "investigation.json").write_text(json.dumps(investigation))
    (tmp_path / "audit.json").write_text(json.dumps(audit))
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "complete"}))
    result = coverage_export.export_bundle(tmp_path)
    graph = result["answer"]
    assert [(c["id"], c["text"], c["evidence_ids"]) for c in graph["claims"]] == [(c["id"], c["text"], c["evidence_ids"]) for c in claims]
    assert [c.get("claim_occurrence") for c in graph["claims"]] == [1, 2, None]
    assert graph["binding_issues"][0]["kind"] == "duplicate_claim_id"
    assert graph["binding_issues"][0]["claim_id"] == "C1" and graph["binding_issues"][0]["record_count"] == 2
    duplicate_checks = [c for c in graph["source_checks"] if c["claim_id"] == "C1"]
    assert [c["claim_occurrence"] for c in duplicate_checks] == [1, 2]
    assert all(c["assessment"] == "not_checked" and c["paragraph_ids"] == [] for c in duplicate_checks)
    assert next(c for c in graph["source_checks"] if c["claim_id"] == "C2")["assessment"] == "supported"
    assert graph["audit"]["status"] == "partial" and graph["audit"]["checked_claims"] == 1
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    assert json.loads((tmp_path / "audit.json").read_text()) == audit
    report = (tmp_path / "answer.md").read_text()
    one, two = report.split("## C1 · recorded occurrence 1", 1)[1].split("## C1 · recorded occurrence 2", 1)
    assert first in one and second not in one
    assert second in two and "Second distinct recorded claim." in two
    assert "Audit: supported" not in one
    assert "Provenance failure: duplicate claim ID C1; ID-only audit checks are not assigned." in one


def test_nested_search_progress_is_exported_unchanged_with_committed_candidates(tmp_path):
    source = tmp_path / "scientist";source.mkdir()
    progress = {"complete": False, "unique_candidates": 1, "pending_query_ids": ["q2"],
                "backoff": {"provider": "openalex", "active": True, "retry_not_before": "2026-10-14T20:00:00Z",
                            "retry_after": "120", "scope": "coverage_operator_shared"}}
    (source / "search-progress.json").write_text(json.dumps(progress))
    (source / "candidates.jsonl").write_text(json.dumps({"id": "W1", "title": "Committed synthetic candidate"}) + "\n")
    (tmp_path / "run.json").write_text(json.dumps({"question": "Synthetic?", "status": "researching"}))
    result = coverage_export.export_bundle(tmp_path, source_dir=source, research_dir=source)
    assert result["coverage"]["search_progress"] == progress
    assert result["coverage"]["discovered_candidates"] == 1
    assert result["run"]["status"] == "in_progress"
    assert dashboard.research_bundle(tmp_path)["coverage"]["search_progress"] == progress
    assert json.loads((source / "search-progress.json").read_text()) == progress


@pytest.mark.parametrize("fails", [False, True])
def test_ask_exports_only_after_its_own_runtime_reservation(tmp_path, monkeypatch, fails):
    workspace = tmp_path / "owned"
    exports = []
    def research(question, directory, **options):
        Path(directory).mkdir()
        options["on_reserved"]()
        (Path(directory) / "run.json").write_text(json.dumps({"question": question, "status": "failed" if fails else "complete"}))
        if fails:
            raise RuntimeError("Offline owned failure")
    monkeypatch.setattr(coverage_runtime, "run", research)
    monkeypatch.setattr(coverage_export, "export_bundle", lambda directory: exports.append(directory))
    assert cli.engine_main(["ask", "Owned question?", "--out", str(workspace)]) == (1 if fails else 0)
    assert exports == [str(workspace)]


def test_ask_pre_reservation_failure_cannot_export_a_competing_run(tmp_path, monkeypatch):
    workspace = tmp_path / "racing"
    exports = []
    def fail_before_reservation(question, directory, **options):
        # Another caller acquires this directory after CLI's preflight.
        Path(directory).mkdir()
        (Path(directory) / "run.json").write_text('{"question":"Other owner?","status":"researching"}')
        raise RuntimeError("Offline pre-reservation failure")
    monkeypatch.setattr(coverage_runtime, "run", fail_before_reservation)
    monkeypatch.setattr(coverage_export, "export_bundle", lambda directory: exports.append(directory))
    assert cli.engine_main(["ask", "New question?", "--out", str(workspace)]) == 1
    assert exports == []
    assert json.loads((workspace / "run.json").read_text())["question"] == "Other owner?"


def test_concurrent_same_question_cli_loser_never_exports_owner_artifacts(tmp_path, monkeypatch):
    workspace = (tmp_path / "same-question").resolve()
    workspace.mkdir()
    barriers = [threading.Barrier(2), threading.Barrier(2)]
    local = threading.local()
    original_iterdir = Path.iterdir
    entered, returned, release = threading.Event(), threading.Event(), threading.Event()
    exports = []
    def synchronized_empty_check(path):
        if path == workspace:
            number = getattr(local, "checks", 0)
            if number < 2:
                local.checks = number + 1
                snapshot = list(original_iterdir(path))
                barriers[number].wait(timeout=5)
                return iter(snapshot)
        return original_iterdir(path)
    def offline_session(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        raise RuntimeError("Offline owned failure")
    def export(directory):
        exports.append(json.loads((Path(directory) / "run.json").read_text())["status"])
        (Path(directory) / "answer.json").write_text('{"owner":"retained partial"}')
    monkeypatch.setattr(Path, "iterdir", synchronized_empty_check)
    monkeypatch.setattr(coverage_runtime.shutil, "which", lambda command: "offline-codex")
    monkeypatch.setattr(coverage_runtime, "scientific_session", offline_session)
    monkeypatch.setattr(coverage_export, "export_bundle", export)
    def attempt():
        code = cli.engine_main(["ask", "Same question?", "--out", str(workspace)])
        returned.set()
        return code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        try:
            assert entered.wait(timeout=5)
            assert returned.wait(timeout=5), "Rejected caller must finish while owner is active"
            assert exports == [], "Rejected caller must not export the owner's active run"
            assert not (workspace / "answer.json").exists()
            assert json.loads((workspace / "run.json").read_text())["status"] == "researching"
        finally:
            release.set()
        assert [future.result(timeout=5) for future in futures] == [1, 1]
    assert exports == ["failed"], "Only the reserved owner exports its retained partial run"
    assert json.loads((workspace / "answer.json").read_text()) == {"owner": "retained partial"}
