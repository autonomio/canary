"""Live usage is cheap; changed science and final provenance stay fully checked."""
import json
import os
from pathlib import Path

import pytest

from canary import coverage_export, coverage_runtime as engine, runs
from canary.candidate_index import CandidateIndex
from test_research_bundle import seed


@pytest.fixture
def progress(tmp_path, monkeypatch):
    seed(tmp_path)
    run = engine.read(tmp_path / 'run.json')
    run.update(status='researching', runtime_status='researching')
    engine.write(tmp_path / 'run.json', run)
    usage = {'available': True, 'totals': {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 10},
             'receipts': [], 'response_count': 1}
    monkeypatch.setattr(engine, 'native_usage', lambda *args: usage)
    state = {}
    def refresh(**options):
        return engine.refresh_committed_progress(tmp_path, 'research', 'gpt-6.1-sol', 'xhigh',
            'fixture-thread', '2026-10-29T00:00:00Z', 20, progress_state=state, **options)
    # The first export may create a source-text alias. Cache only a stable input
    # snapshot, never an input that changed during the preceding export.
    for _ in range(3):
        refresh()
        if state.get('inputs') is not None: break
    assert state.get('inputs') is not None
    return tmp_path, refresh, state, usage


def test_usage_only_refresh_does_not_open_papers_or_rewrite_findings(progress, monkeypatch):
    root, refresh, state, usage = progress
    findings = (root / 'answer.json').read_bytes()
    previous = engine.read(root / 'profile.json')['progress']['findings_updated_at']
    original = Path.read_bytes
    def read(path):
        assert path.parent.name not in ('papers', 'texts'), 'Usage-only tick reopened source body'
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', read)
    monkeypatch.setattr(coverage_export, 'export_bundle', lambda *args: pytest.fail('Unchanged findings rebuilt'))
    usage['totals']['input_tokens'] = 150
    assert refresh() is None
    assert (root / 'answer.json').read_bytes() == findings
    profile = engine.read(root / 'profile.json')
    assert profile['progress']['findings_refreshed'] is False
    assert profile['progress']['findings_updated_at'] == previous
    assert profile['roles']['research']['tokens']['input_tokens'] == 150
    metrics = runs._bundle_log_metrics(root, engine.read(root/'run.json'), {}, [], {})
    assert metrics['input_tokens'] == 150 and metrics['cached_tokens'] == 80


@pytest.mark.parametrize('input_name', ['investigation.json', 'selection.json', 'audit.json',
    'passages/W1.jsonl', 'texts/W1.txt', 'papers/W1.txt', '.candidate-index.sqlite'])
def test_changed_scientific_inputs_invalidate_progress_projection(progress, input_name):
    root, refresh, state, _ = progress
    if input_name == '.candidate-index.sqlite':
        with CandidateIndex(root) as index:
            index.add_page([{'id': 'https://openalex.org/W2', 'display_name': 'New candidate'}], 'q1')
    else:
        path = root / input_name
        if path.suffix == '.jsonl': path.write_bytes(path.read_bytes() * 2)
        elif path.exists(): path.write_bytes(path.read_bytes() + b'\n')
        else: engine.write(path, {})
    assert refresh() is not None
    assert engine.read(root/'profile.json')['progress']['findings_refreshed'] is True


def test_final_refresh_rehashes_even_if_body_metadata_is_unchanged(progress):
    root, refresh, state, _ = progress
    paper = root / 'papers/W1.txt'
    stat = paper.stat()
    raw = paper.read_bytes()
    # Simulate a source altered without changing the metadata-based live hint.
    paper.write_bytes(raw[:-1] + b'!')
    os.utime(paper, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    result = refresh(finished=True)
    assert any(i['kind'] == 'body_hash_mismatch' for i in result['answer']['binding_issues'])
    assert engine.read(root/'profile.json')['progress']['final_refresh'] is True


def test_source_change_during_export_is_not_cached_as_published(progress, monkeypatch):
    root, refresh, state, _ = progress
    original = coverage_export.export_bundle
    calls = []
    def export(workspace):
        calls.append(True)
        result = original(workspace)
        if len(calls) == 1:
            raw = engine.read(root/'investigation.json')
            raw['claims'][0]['text'] = 'Newly committed wording.'
            engine.write(root/'investigation.json', raw)
        return result
    monkeypatch.setattr(coverage_export, 'export_bundle', export)
    refresh(finished=True)
    assert state['inputs'] is None
    result = refresh()
    assert len(calls) == 2 and result['answer']['claims'][0]['text'] == 'Newly committed wording.'
