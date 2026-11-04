"""Audited recommendations stay distinct and survive API/export/retention."""
import copy
import json

import jsonschema
import pytest

from canary import (bundle_connectors, claim_uncertainty as uncertainty, compact_bundle,
                    coverage_export, coverage_protocol, research_bundle, research_spec)
from test_api_contract import api, validate_response
from test_coverage_evidence_ui import run_dashboard
from test_research_bundle import seed


STATE = {'claims': [{'id': 'C-prior', 'statement': 'A synthetic attributed finding.',
                     'uncertainty': 'high'}]}
REVIEW = {'recommended_class': 'moderate', 'reason': 'New inspected E1 addresses one prior limitation.',
          'evidence_ids': ['E1'], 'prior_claim_id': 'C-prior', 'prior_class': 'high',
          'prior_match_reason': 'Same proposition, population, conditions and material scope.'}
CHECK = {'claim_id': 'C1', 'assessment': 'supported', 'reason': 'Synthetic source check.',
         'passage_ids': ['W1-p0001'], 'reference_ids': ['W1'], 'context_checked': ['methods'],
         'uncertainty_review': REVIEW}


@pytest.mark.parametrize('prior,recommended,expected', [
    ('low', 'high', 'increased'), ('medium', 'moderate', 'unchanged'),
    ('high', 'moderate', 'decreased')])
def test_change_is_derived_from_supplied_grade(prior, recommended, expected):
    state = copy.deepcopy(STATE); state['claims'][0]['uncertainty'] = prior
    check = copy.deepcopy(CHECK)
    check['uncertainty_review'].update(prior_class='moderate' if prior == 'medium' else prior,
                                      recommended_class=recommended, change='fabricated')
    before = copy.deepcopy((state, check))
    result = uncertainty.review(check, state=state, evidence_ids=['E1'])
    assert result['status'] == 'assessed' and result['change'] == expected
    assert (state, check) == before


@pytest.mark.parametrize('state,patch', [
    (None, {}), ({'claims': []}, {}), ({'claims': STATE['claims'] * 2}, {}),
    (STATE, {'prior_class': 'low'}), (STATE, {'prior_match_reason': ''}),
    (STATE, {'evidence_ids': []})])
def test_unverifiable_comparison_is_not_reported_as_a_change(state, patch):
    check = copy.deepcopy(CHECK); check['uncertainty_review'].update(patch)
    result = uncertainty.review(check, state=state, evidence_ids=['E1'])
    assert result['status'] == 'assessed' and result['change'] == 'not_comparable'
    assert result['limitations']


@pytest.mark.parametrize('check', [{}, {'assessment': 'supported'},
    dict(CHECK, assessment='not_checked'),
    dict(CHECK, uncertainty_review={k: v for k, v in REVIEW.items() if k != 'reason'}),
    dict(CHECK, uncertainty_review=dict(REVIEW, recommended_class=[])),
    dict(CHECK, uncertainty_review=dict(REVIEW, evidence_ids=['missing'])),
    dict(CHECK, uncertainty_review=dict(REVIEW, evidence_ids=['E1', {}]))])
def test_missing_unchecked_and_malformed_reviews_never_create_a_grade(check):
    result = uncertainty.review(check, state=STATE, evidence_ids=['E1'])
    assert result['status'] == 'not_assessed' and result['recommended_class'] is None
    assert result['change'] == 'not_comparable'


def test_generation_contract_and_same_audit_prompt():
    schema = coverage_protocol.audit_schema()['properties']['claim_checks']['items']
    jsonschema.Draft202012Validator(schema).validate(CHECK)
    legacy = dict(CHECK); legacy.pop('uncertainty_review')
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(legacy)
    prompt = coverage_protocol.audit_prompt('Synthetic question?', 'canary tools')
    for required in ('uncertainty_review', 'not a probability', 'prior_match_reason',
                     'freshness policy', 'never copy a default grade', 'No audit time limit'):
        assert required in prompt
    assert 'Never rewrite' in prompt


def prepare(path, *, initialize=True):
    packet = research_spec.compile_mapping({'question': 'Synthetic audit?', 'research_state': STATE,
                                            'continue_research': True})
    if initialize:
        research_bundle.initialize(path, packet)
    payload = seed(path)
    audit = {'claim_checks': [copy.deepcopy(CHECK)], 'audit_complete': True,
             'scope_note': 'Synthetic audit, not scientific evidence.'}
    payload['independent_audit'] = audit
    research_bundle.write_json(path / 'payload.json', payload)
    research_bundle.write_json(path / 'audit.json', audit)
    return payload


def test_projection_export_ui_and_compact_retention(tmp_path):
    payload = prepare(tmp_path)
    original = copy.deepcopy(payload['claims'][0]['uncertainty'])
    graph = coverage_export.export_bundle(tmp_path)['answer']
    reviewed = graph['source_checks'][0]['uncertainty_review']
    assert reviewed['change'] == 'decreased' and reviewed['prior_class'] == 'high'
    assert graph['claims'][0]['uncertainty'] == original
    assert 'Auditor uncertainty recommendation: moderate' in (tmp_path / 'answer.md').read_text()
    research_bundle.seal(tmp_path)
    exported = bundle_connectors.resolvent(tmp_path)
    metadata = exported['method_metadata']['claim_metadata']['c1']
    assert exported['claims'][0]['uncertainty'] == 'medium'
    assert metadata['uncertainty'] == original
    assert metadata['uncertainty_review']['evidence_ids'] == ['e1']
    assert metadata['uncertainty_review']['original_evidence_ids'] == ['E1']
    assert metadata['uncertainty_review']['prior_claim_id'] == 'C-prior'
    assert metadata['uncertainty_review']['disposition'] == 'requires_destination_review'
    assert 'accept, override or defer' in exported['handoff']['summary']
    run_dashboard(tmp_path, graph, r'''
const original=JSON.stringify(graph),html=claimInspector(graph,'C1');
assert.match(html,/Auditor uncertainty recommendation/);
assert.match(html,/moderate uncertainty · decreased from supplied prior/);
assert.match(html,/C-prior · auditor-reported high uncertainty/);
assert.match(html,/Writer’s grade retained/);
assert.match(html,/data-qa-evidence="E1"/);
assert.equal(JSON.stringify(graph),original);
''')
    (tmp_path / '.run-reservation').touch()
    compact_bundle.retain(tmp_path)
    assert not (tmp_path / 'audit.json').exists()
    assert bundle_connectors.resolvent(tmp_path)['method_metadata']['claim_metadata'] == exported['method_metadata']['claim_metadata']
    saved = compact_bundle.projection(tmp_path)['answer']
    assert saved['source_checks'][0]['uncertainty_review'] == reviewed


def test_legacy_and_duplicate_audit_records_are_unassessed(tmp_path):
    prepare(tmp_path)
    (tmp_path / 'audit.json').write_text(json.dumps({'claim_checks': [CHECK, CHECK]}))
    graph = coverage_export.export_bundle(tmp_path)['answer']
    assert graph['source_checks'][0]['uncertainty_review']['status'] == 'not_assessed'
    (tmp_path / 'audit.json').write_text(json.dumps({'claim_checks': [dict(CHECK, uncertainty_review=None)]}))
    graph = coverage_export.export_bundle(tmp_path)['answer']
    run_dashboard(tmp_path, graph, r'''
const html=claimInspector(graph,'C1');
assert.match(html,/Auditor uncertainty recommendation<\/h3><p>Not assessed/);
assert.doesNotMatch(html,/decreased from supplied prior/);
''')


def test_review_is_typed_and_visible_over_the_existing_http_api(api):
    _, client, root = api
    response = client.post('/api/runs', json={'spec': {'question': 'Synthetic review transport?',
        'research_state': STATE, 'continue_research': True}})
    assert response.status_code == 201, response.text
    workspace = root / 'synthetic'
    prepare(workspace, initialize=False)
    coverage_export.export_bundle(workspace); research_bundle.seal(workspace)
    response = client.get('/api/runs/synthetic/research')
    assert response.status_code == 200
    contract = client.get('/openapi.json').json()
    validate_response(contract, '/api/runs/{key}/research', 'get', 200, response.json())
    review = response.json()['research']['answer']['source_checks'][0]['uncertainty_review']
    assert review['recommended_class'] == 'moderate' and review['change'] == 'decreased'
    result = client.get('/api/runs/synthetic/exports/resolvent?representation=expanded')
    assert result.status_code == 200, result.text
    assert result.json()['method_metadata']['claim_metadata']['c1']['uncertainty_review']['change'] == 'decreased'


def test_audit_low_recommendation_never_promotes_candidate_or_writer(tmp_path):
    payload = prepare(tmp_path)
    payload['claims'][0]['uncertainty'] = {'class': 'high', 'score': 3, 'rationale': 'Writer limitation.'}
    payload['independent_audit']['claim_checks'][0]['uncertainty_review']['recommended_class'] = 'low'
    research_bundle.write_json(tmp_path / 'payload.json', payload)
    research_bundle.seal(tmp_path)
    exported = bundle_connectors.resolvent(tmp_path)
    assert exported['claims'][0]['uncertainty'] == 'high'
    metadata = exported['method_metadata']['claim_metadata']['c1']
    assert metadata['uncertainty']['class'] == 'high'
    assert metadata['uncertainty_review']['recommended_class'] == 'low'
    assert exported['evidence'][0]['independent'] is False


@pytest.mark.parametrize('audit', [None, 'legacy', {'claim_checks': None},
                                   {'claim_checks': ['legacy row', {'claim_id': []}]}])
def test_legacy_audit_shapes_do_not_block_export(tmp_path, audit):
    payload = prepare(tmp_path)
    payload['independent_audit'] = audit
    research_bundle.write_json(tmp_path / 'payload.json', payload)
    research_bundle.seal(tmp_path)
    result = bundle_connectors.resolvent(tmp_path)
    assert result['method_metadata']['independent_audit'] == audit
    assert result['method_metadata']['claim_metadata']['c1']['uncertainty_review']['status'] == 'not_assessed'


def test_conflicting_audit_rows_are_explicit_partial_and_retained(tmp_path):
    payload = prepare(tmp_path)
    checks = [copy.deepcopy(CHECK), dict(copy.deepcopy(CHECK), assessment='correction_needed',
                                       reason='The source requires a material correction.')]
    audit = dict(payload['independent_audit'], claim_checks=checks, audit_complete=True)
    payload['independent_audit'] = audit
    research_bundle.write_json(tmp_path / 'payload.json', payload)
    research_bundle.write_json(tmp_path / 'audit.json', audit)
    graph = coverage_export.export_bundle(tmp_path)['answer']
    assert graph['audit']['status'] == 'partial' and graph['audit']['checked_claims'] == 0
    check = graph['source_checks'][0]
    assert check['assessment'] == 'not_checked' and 'Multiple recorded audit checks' in check['reason']
    assert check['recorded_checks'] == checks
    assert check['uncertainty_review']['status'] == 'not_assessed'
    assert check['paragraph_ids'] == ['W1-p0001']
    assert any(i['kind'] == 'duplicate_audit_check' and i['record_count'] == 2 for i in graph['binding_issues'])
    assert 'correction_needed; The source requires a material correction.' in (tmp_path / 'answer.md').read_text()
    assert json.loads((tmp_path / 'audit.json').read_text()) == audit
    run_dashboard(tmp_path, graph, r'''
assert.equal(graph.audit.status,'partial');
assert.match(findingHtml(graph,graph.claims[0]),/Provenance failure/);
const html=claimInspector(graph,'C1');
assert.match(html,/Multiple recorded audit checks/);
assert.match(html,/Recorded check 1 · Supported/);
assert.match(html,/Recorded check 2 · Correction needed/);
assert.match(html,/The source requires a material correction/);
assert.match(html,/data-qa-passage="W1-p0001"/);
''')
    research_bundle.seal(tmp_path)
    exported = bundle_connectors.resolvent(tmp_path)
    assert exported['method_metadata']['independent_audit'] == audit
    assert 'Multiple recorded audit checks' in exported['method_metadata']['claim_metadata']['c1']['uncertainty_review']['reason']
    (tmp_path / '.run-reservation').touch()
    compact_bundle.retain(tmp_path)
    retained = compact_bundle.projection(tmp_path)['answer']
    assert retained['audit']['status'] == 'partial'
    assert retained['source_checks'][0]['recorded_checks'] == checks


def test_new_uncertainty_logic_is_in_engine_fingerprints(tmp_path):
    from pathlib import Path
    prepare(tmp_path); research_bundle.seal(tmp_path)
    engine = research_bundle.read(tmp_path)['specification']['engine']
    assert engine['sources']['claim_uncertainty.py'] == research_bundle.file_hash(Path(uncertainty.__file__))
