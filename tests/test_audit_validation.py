"""Frozen synthetic component fixtures; no model call or live scientific claim."""
from copy import deepcopy
import json

import jsonschema
import pytest

from canary import api_contract, audit_validation as audit, coverage_protocol as protocol, coverage_export
from test_research_bundle import seed, completed
from test_coverage_evidence_ui import run_dashboard


def review():
    return {'claim_checks': [{'claim_id': 'C1', 'assessment': 'supported', 'reason': 'Synthetic inspected scope.',
        'passage_ids': ['W1-p0001'], 'reference_ids': ['W1'], 'context_checked': ['Synthetic methods/results.'], 'issue_types': []}],
        'evidence_checks': [{'evidence_id': 'E1', 'assessment': 'independent', 'reason': 'Synthetic family review.',
            'reference_ids': ['W1'], 'passage_ids': ['W1-p0001'], 'study_family_ids': ['fixture-family'],
            'family_basis': 'Synthetic study identity in the inspected record.'}],
        'findings': [], 'scores': {k: {'score': 3, 'reason': 'Fixture.'} for k in (
            'selection_defensibility', 'question_relevance', 'scientific_fidelity', 'evidence_coverage', 'information_value')},
        'source_reviewed_ids': ['W1'], 'selection_assessment': 'Fixture.', 'strengths': [], 'limitations': [],
        'audit_complete': True, 'scope_note': 'Synthetic only.'}


def test_generation_protocol_and_recorded_api_contract(tmp_path):
    payload = seed(tmp_path)
    raw = review()
    jsonschema.validate(raw, protocol.audit_schema())
    bound = audit.bind(payload, raw)
    assert bound['audit_complete']
    assert raw == review(), 'The raw reviewer response is immutable'
    assert audit.current(payload, payload['claims'][0], bound['claim_checks'][0])
    contract = api_contract.schemas()
    for value, name in [(bound['claim_checks'][0], 'RecordedClaimCheck'), (bound['evidence_checks'][0], 'EvidenceIndependence')]:
        jsonschema.validate(value, {'$ref': '#/components/schemas/'+name, 'components': {'schemas': contract}})


@pytest.mark.parametrize('edit', ['text', 'evidence', 'result', 'source', 'shared', 'coverage_map'])
def test_any_scientific_input_edit_invalidates_verdict(tmp_path, edit):
    payload = seed(tmp_path)
    check = audit.bind(payload, review())['claim_checks'][0]
    if edit == 'text': payload['claims'][0]['text'] += ' Changed.'
    elif edit == 'evidence': payload['evidence'][0]['interpretation'] += ' Changed.'
    elif edit == 'result': payload['study_results'].append({'id': 'S1', 'fields': {}})
    elif edit == 'source': payload['references'][0]['body_sha256'] = '0'*64
    elif edit == 'coverage_map': payload['coverage_map'] = {'changed': True}
    else: payload['contradictions'].append({'id': 'X1', 'description': 'Shared change.'})
    assert not audit.current(payload, payload['claims'][0], check)


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'unknown', 'invalid_passage', 'no_context', 'malformed'])
def test_bad_or_absent_checks_never_count_as_complete(tmp_path, defect):
    payload, raw = seed(tmp_path), review()
    if defect == 'missing': raw['claim_checks'] = []
    elif defect == 'duplicate': raw['claim_checks'] *= 2
    elif defect == 'unknown': raw['claim_checks'][0]['claim_id'] = 'S1'
    elif defect == 'invalid_passage': raw['claim_checks'][0]['passage_ids'] = ['missing']
    elif defect == 'no_context': raw['claim_checks'][0]['context_checked'] = []
    else: raw['claim_checks'][0]['passage_ids'] = None
    bound = audit.bind(payload, raw)
    assert not bound['audit_complete']
    assert len(bound['claim_checks']) == 1
    assert bound['claim_checks'][0]['assessment'] == 'not_checked'


def test_scope_defects_require_correction_even_if_reviewer_says_supported(tmp_path):
    payload, raw = seed(tmp_path), review()
    raw['claim_checks'][0]['issue_types'] = ['missing_scope']
    assert audit.bind(payload, raw)['claim_checks'][0]['assessment'] == 'correction_needed'


@pytest.mark.parametrize('defect', ['no_review', 'no_family', 'no_basis', 'no_address', 'unrelated', 'duplicate'])
def test_independence_requires_explicit_inspected_source_and_family_basis(tmp_path, defect):
    payload, raw = seed(tmp_path), review()
    check = raw['evidence_checks'][0]
    if defect == 'no_review': raw['source_reviewed_ids'] = []
    elif defect == 'no_family': check['study_family_ids'] = []
    elif defect == 'no_basis': check['family_basis'] = ''
    elif defect == 'no_address': payload['references'][0]['url'] = None
    elif defect == 'unrelated': check['passage_ids'] = ['missing']
    else: raw['evidence_checks'] *= 2
    assert audit.bind(payload, raw)['evidence_checks'][0]['assessment'] == 'unknown'


def test_export_and_dashboard_show_verdict_binding_defects_and_independence(tmp_path):
    _, payload = completed(tmp_path)
    raw = review(); raw['claim_checks'][0]['issue_types'] = ['missing_scope']
    bound = audit.bind(payload, raw)
    (tmp_path/'audit.json').write_text(json.dumps(bound))
    coverage_export.export_bundle(tmp_path)
    graph = json.loads((tmp_path/'answer.json').read_text())
    assert graph['source_checks'][0]['assessment'] == 'correction_needed'
    run_dashboard(tmp_path, graph, """
assert.match(sourceCheckHtml(graph,graph.claims[0]),/missing scope/);
assert.match(sourceCheckHtml(graph,graph.claims[0]),/statement SHA-256/);
assert.match(evidenceIndependenceHtml(graph),/fixture|Synthetic/);
assert.match(evidenceIndependenceHtml(graph),/separate auditor or different URLs/);
""")
    payload['evidence'][0]['interpretation'] += ' Changed.'
    (tmp_path/'payload.json').write_text(json.dumps(payload))
    coverage_export.export_bundle(tmp_path)
    graph = json.loads((tmp_path/'answer.json').read_text())
    assert graph['source_checks'][0]['assessment'] == 'not_checked'
    assert graph['audit']['status'] == 'partial'
    assert graph['evidence_independence'][0]['assessment'] == 'unknown'
    assert 'changed since this verdict' in graph['source_checks'][0]['reason']


def test_shared_rules_require_standalone_claims_and_unlimited_fresh_audit():
    prompt = protocol.audit_prompt('Synthetic question?', 'canary tools')
    for text in ('stand alone', 'one proposition', 'recommendation', 'source_record', 'issue_types',
                 'one evidence_checks entry', 'No audit time limit', 'Never inspect researcher reasoning'):
        assert text in prompt



def test_missing_independence_scope_cannot_report_a_finished_audit(tmp_path):
    payload, raw = seed(tmp_path), review()
    raw['evidence_checks'] = []
    bound = audit.bind(payload, raw)
    assert not bound['audit_complete']
    assert bound['evidence_checks'][0]['assessment'] == 'unknown'


@pytest.mark.parametrize('field', ['evidence', 'study_results', 'claims', 'gaps', 'contradictions'])
def test_edits_to_the_rendered_investigation_invalidate_old_verdicts(tmp_path, field):
    _, payload = completed(tmp_path)
    (tmp_path/'audit.json').write_text(json.dumps(audit.bind(payload, review())))
    raw = json.loads((tmp_path/'investigation.json').read_text())
    if field == 'evidence': raw[field][0]['interpretation'] += ' Changed.'
    elif field == 'study_results': raw[field] = [{'id': 'S1', 'fields': {}}]
    elif field == 'claims': raw[field].append(dict(raw[field][0], id='C2'))
    elif field == 'gaps': raw[field][0]['description'] += ' Changed.'
    else: raw[field] = [{'id': 'X1', 'claim_ids': [], 'evidence_ids': []}]
    (tmp_path/'investigation.json').write_text(json.dumps(raw))
    coverage_export.export_bundle(tmp_path)
    graph = json.loads((tmp_path/'answer.json').read_text())
    assert graph['audit']['status'] == 'partial'
    assert all(c['assessment'] == 'not_checked' for c in graph['source_checks'])


@pytest.mark.parametrize('assessment', ['same_study_family', 'independent'])
def test_independence_can_cite_inspected_sibling_source_facts(tmp_path, assessment):
    payload, raw = seed(tmp_path), review()
    reference = dict(payload['references'][0], id='W2')
    passage = dict(payload['evidence'][0]['verbatims'][0], id='W2-p0001', reference_id='W2')
    payload['references'].append(reference)
    payload['evidence'].append(dict(deepcopy(payload['evidence'][0]), id='E2', passage_ids=[passage['id']],
        reference_ids=['W2'], verbatims=[passage]))
    raw['source_reviewed_ids'].append('W2')
    raw['evidence_checks'][0].update(assessment=assessment, reference_ids=['W1', 'W2'], passage_ids=['W1-p0001', 'W2-p0001'])
    assert audit.bind(payload, raw)['evidence_checks'][0]['assessment'] == assessment


def test_dashboard_distinguishes_historical_scope_validation_failures_and_nonempirical_records(tmp_path):
    _, payload = completed(tmp_path)
    raw = review(); raw['claim_checks'][0]['context_checked'] = []
    (tmp_path/'audit.json').write_text(json.dumps(audit.bind(payload, raw)))
    coverage_export.export_bundle(tmp_path)
    graph = json.loads((tmp_path/'answer.json').read_text())
    run_dashboard(tmp_path, graph, """
const data={run:{selection_method:'coverage-guided'},answer:graph};
assert.match(coverageAuditHtml(data),/stand alone/);
assert.match(sourceCheckHtml(graph,graph.claims[0]),/Audit output validation/);
const old={...graph,audit:{...graph.audit,standard_version:null}};
assert.doesNotMatch(coverageAuditHtml({...data,answer:old}),/stand alone/);
for(const kind of ['recommendation','source_record']) {
  const c={...graph.claims[0],kind};
  assert.match(claimRecordHtml(graph,c,''),new RegExp(kind.replaceAll('_',' ')));
}
""")
    graph['claims'][0]['kind'] = 'recommendation'
    assert 'Record kind: recommendation' in coverage_export.render_markdown(graph)
