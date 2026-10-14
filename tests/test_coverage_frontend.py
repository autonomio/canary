"""Deterministic production entrypoint, source-context and operational receipts."""
import json
import sqlite3
from pathlib import Path

import pytest

from canary import cli, coverage_runtime, coverage_export, dashboard, runs


def test_ask_routes_to_coverage_without_touching_legacy_review(tmp_path, monkeypatch):
    calls = []
    def research(question, workspace, **options):
        calls.append((question, workspace, options))
        Path(workspace).mkdir()
        (Path(workspace) / "run.json").write_text('{}')
    monkeypatch.setattr(coverage_runtime, "run", research)
    monkeypatch.setattr(coverage_export, "export_bundle", lambda directory: calls.append(directory))
    monkeypatch.setattr(cli, "review", lambda *a, **k: pytest.fail("legacy review invoked"))
    workspace = tmp_path / "new"
    assert cli.main(["ask", "Question?", "--out", str(workspace)]) == 0
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
    monkeypatch.setattr(dashboard, "QUESTION_ENGINE_ROOT", tmp_path)
    path = tmp_path / "out" / "coverage-openalex-usage.sqlite"
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE searches(at TEXT NOT NULL)")
        db.executemany("INSERT INTO searches VALUES(?)", [(runs.utcnow(),), (runs.utcnow(),), ("2000-01-01T00:00:00Z",)])
    result = dashboard.openalex_status()
    assert result["keyword_searches_today"] == 2
    assert "gateway and other clients excluded" in result["count_scope"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 3


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
    assert cli.main(["ask", "New question?", "--out", str(old)]) == 1
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
    names = ("qaGraph", "active", "answerHtml")
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
    runtime.write(tmp_path / "reviewed-papers.json", reviews)
    runtime.write(tmp_path / "selection.json", {"selected_for_review_ids": ["W1", "W2", "W3"]})
    runtime.write(tmp_path / "run.json", {"question": "Synthetic?", "minimum_papers": 50})
    runtime.write(tmp_path / "investigation.json", {"claims": [], "evidence": [], "study_results": []})
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
