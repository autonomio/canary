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
