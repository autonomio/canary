"""Malformed source records are refused early and remain inspectable in old runs."""
import json

import pytest

from canary import (bundle_connectors, coverage_export, coverage_protocol, coverage_runtime as engine,
                    research_bundle, research_spec, research_tools, runs, source_adapters)
from test_api_contract import api, validate_response  # noqa: F401 (fixture)
from test_coverage_evidence_ui import run_dashboard
from test_research_bundle import seed


def source_record(reference):
    return dict(reference, adapter='osf', source_kind='study_registration',
                record_url='https://osf.io/abc12', source_metadata={'native_id': 'abc12'},
                full_text_status='record_requires_source_verification')


@pytest.mark.parametrize('field,value', [
    ('adapter', None), ('source_kind', ''), ('record_url', None),
    ('record_url', '  '), ('source_metadata', None), ('source_metadata', []),
    ('source_kind', 'local_verification_record'),
])
def test_finalize_refuses_invalid_source_before_changing_investigation(tmp_path, field, value):
    payload = seed(tmp_path)
    reference = source_record(payload['references'][0])
    reference[field] = value
    engine.write(tmp_path / 'download-receipts/W1.json', {'reference': reference})
    before = (tmp_path / 'investigation.json').read_bytes()
    with pytest.raises(ValueError, match=field):
        research_tools.execute({'bundle': str(tmp_path), 'status': 'researching'}, 'finalize',
                               {'investigation': {'claims': []}}, tmp_path)
    assert (tmp_path / 'investigation.json').read_bytes() == before


def test_acquisition_refuses_agent_generated_verification_before_fetching(tmp_path):
    identifier = source_adapters.identifier('offline_code_execution', 'synthetic')
    work = {'id': identifier, 'display_name': 'Synthetic offline verification',
            'adapter': 'offline_code_execution', 'source_kind': 'local_verification_record',
            'source_metadata': {}, 'record_url': None}
    engine.write(tmp_path / 'works.json', [work])
    def unexpected_fetch(*args):
        pytest.fail('An inadmissible source must not be fetched')
    receipts = engine.acquire(tmp_path, [identifier], fetch=unexpected_fetch)
    assert receipts[0]['status'] == 'retrieval_or_extraction_failed'
    assert 'agent-generated verification' in receipts[0]['reason']
    assert 'record_url' in receipts[0]['reason']
    assert engine.catalog(tmp_path) == []
    assert not (tmp_path / 'papers').exists()


@pytest.mark.parametrize('missing', ['adapter', 'source_metadata', 'record_url', 'full_text_status'])
def test_stored_incomplete_source_survives_progress_final_and_connector_exports(tmp_path, missing):
    research_bundle.initialize(tmp_path, research_spec.compile_mapping({'question': 'Synthetic fixture question?'}))
    payload = seed(tmp_path)
    reference = source_record(payload['references'][0])
    reference.pop(missing)
    reference['url'] = None
    reference['doi'] = None
    payload['references'] = [reference]
    engine.write(tmp_path / 'payload.json', payload)
    engine.write(tmp_path / 'references.json', [reference])
    engine.write(tmp_path / 'download-receipts/W1.json', {'reference': reference})
    before = (tmp_path / 'download-receipts/W1.json').read_bytes()
    result = engine.refresh_committed_progress(tmp_path, 'research', 'synthetic-model', 'high',
                                               None, engine.now(), 1, progress_state={})
    assert result is not None
    view = coverage_export.export_bundle(tmp_path)
    exported = view['answer']['references'][0]
    assert exported['paper']['url'] == reference.get('record_url')
    assert view['answer']['evidence'][0]['quote'] == payload['evidence'][0]['verbatims'][0]['text']
    assert (tmp_path / 'download-receipts/W1.json').read_bytes() == before
    research_bundle.seal(tmp_path)
    for connector in bundle_connectors.EXPORTERS:
        handoff = bundle_connectors.export(tmp_path, connector, representation='expanded')
        assert len(handoff['sources']) == 1
        assert handoff['sources'][0]['uri'] == reference.get('record_url')
        assert handoff['sources'][0]['excerpt'] == payload['evidence'][0]['verbatims'][0]['text']
        locator = json.loads(handoff['sources'][0]['locator'])
        assert locator['source_kind'] == 'study_registration'
        if missing != 'full_text_status':
            assert any(missing in issue for issue in locator['source_record_issues'])
    if missing != 'full_text_status':
        assert any(missing in issue for issue in exported['paper']['extra']['source_record_issues'])
        assert missing in (tmp_path / 'answer.md').read_text()
        run_dashboard(tmp_path, exported, r"""
const html=qaCitation(graph);
assert.match(html,/role="alert"/);
assert.match(html,/Invalid source record/);
assert.match(html,/adapter|source_metadata|record_url/);
assert.doesNotMatch(html,/openalex.org/);
""")


def test_offline_source_is_visible_as_inadmissible_without_inventing_a_url(tmp_path):
    payload = seed(tmp_path)
    reference = dict(payload['references'][0], adapter='offline_code_execution',
                     source_kind='local_verification_record', url=None, doi=None)
    engine.write(tmp_path / 'references.json', [reference])
    graph = coverage_export.export_bundle(tmp_path)['answer']
    extra = graph['references'][0]['paper']['extra']
    assert graph['references'][0]['paper']['url'] is None
    assert any('agent-generated verification' in issue for issue in extra['source_record_issues'])
    row = coverage_protocol.agent_reference(reference)
    assert row['source_record_issues'] == extra['source_record_issues']
    run_dashboard(tmp_path, graph, r"""
const html=qaCitation(graph.references[0]);
assert.match(html,/agent-generated verification/);
assert.match(html,/record_url/);
assert.doesNotMatch(html,/openalex.org/);
""")


@pytest.mark.parametrize('field,value', [('record_url', None), ('source_metadata', []),
                                         ('source_kind', 'local_verification_record')])
def test_http_reports_source_errors_and_accepts_a_corrected_record(api, tmp_path, field, value):
    server, client, _ = api
    workspace = tmp_path / 'source-validation'
    workspace.mkdir()
    payload = seed(workspace)
    valid = source_record(payload['references'][0])
    engine.write(workspace / 'run.json', dict(engine.read(workspace / 'run.json'), status='researching'))
    engine.write(workspace / 'download-receipts/W1.json', {'reference': dict(valid, **{field: value})})
    registry = server.RequestHandlerClass.supervisor.registry
    runs.register_start('source-validation', 'Synthetic source validation', '', kind='question',
                        bundle=str(workspace), status='researching', path=registry)
    contract = client.get('/openapi.json').json()
    catalog = client.post('/api/runs/source-validation/tools/catalog', json={})
    assert catalog.status_code == 200, catalog.text
    validate_response(contract, '/api/runs/{key}/tools/catalog', 'post', 200, catalog.json())
    assert any(field in issue for issue in catalog.json()[0]['source_record_issues'])
    before = (workspace / 'investigation.json').read_bytes()
    response = client.post('/api/runs/source-validation/tools/finalize', json={'investigation': {'claims': []}})
    assert response.status_code == 400, response.text
    validate_response(contract, '/api/runs/{key}/tools/finalize', 'post', 'default', response.json())
    assert 'W1' in response.json()['error'] and field in response.json()['error']
    assert (workspace / 'investigation.json').read_bytes() == before
    engine.write(workspace / 'download-receipts/W1.json', {'reference': valid})
    response = client.post('/api/runs/source-validation/tools/finalize', json={})
    assert response.status_code == 200, response.text
