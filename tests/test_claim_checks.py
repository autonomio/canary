"""Claim checks run against frozen synthetic component inputs."""
from copy import deepcopy
import json

import jsonschema
import pytest

from canary import api_contract, audit_validation, claim_checks, coverage_export
from test_research_bundle import completed, seed
from test_audit_validation import review
from test_coverage_evidence_ui import run_dashboard


def test_empirical_claim_needs_an_actual_linked_result(tmp_path):
    payload = seed(tmp_path)
    assert claim_checks.check(payload)[0]['blocking']
    payload['claims'][0]['study_result_ids'] = ['missing']
    assert claim_checks.check(payload)[0]['kind'] == 'missing_study_result'
    payload['study_results'] = [{'id': 'missing', 'fields': {}}]
    assert claim_checks.check(payload) == []


@pytest.mark.parametrize('kind', ['recommendation', 'source_record', 'author_interpretation', 'derived_comparison'])
def test_nonempirical_claims_do_not_require_invented_results(tmp_path, kind):
    payload = seed(tmp_path)
    payload['claims'][0]['kind'] = kind
    assert claim_checks.check(payload) == []


def test_scope_cue_does_not_override_the_auditors_supported_judgment(tmp_path):
    payload = seed(tmp_path)
    payload['claims'][0].update(kind='author_interpretation', text='That same synthetic fixture.')
    payload['claim_review_checks'] = claim_checks.check(payload)
    assert payload['claim_review_checks'][0]['blocking'] is False
    assert audit_validation.bind(payload, review())['claim_checks'][0]['assessment'] == 'supported'
    payload['claims'][0]['kind'] = 'reported_finding'
    payload['claim_review_checks'] = claim_checks.check(payload)
    assert audit_validation.bind(payload, review())['claim_checks'][0]['assessment'] == 'correction_needed'


def test_possible_duplicate_requires_matching_source_value_and_context_and_never_merges(tmp_path):
    payload = seed(tmp_path)
    payload['study_results'] = [{'id': 'S1', 'reference_ids': ['W1'], 'result_type': 'measured',
        'fields': {'value': {'text': 'Synthetic 17', 'passage_ids': ['W1-p0001']},
                   'endpoint': {'text': 'Synthetic endpoint A', 'passage_ids': ['W1-p0001']}},
        'version_or_cohort_note': 'Fixture cohort.'}]
    payload['claims'][0]['study_result_ids'] = ['S1']
    payload['claims'].append(dict(deepcopy(payload['claims'][0]), id='C2'))
    original = deepcopy(payload)
    records = claim_checks.check(payload)
    assert len(records) == 1 and records[0]['kind'] == 'possible_duplicate' and not records[0]['blocking']
    assert records[0]['claim_ids'] == ['C1', 'C2']
    assert payload == original
    other = dict(deepcopy(payload['study_results'][0]), id='S2')
    other['fields']['endpoint']['text'] = 'Synthetic endpoint B'
    payload['study_results'].append(other); payload['claims'][1]['study_result_ids'] = ['S2']
    assert claim_checks.check(payload) == []


def test_api_and_dashboard_expose_checks_with_claim_navigation(tmp_path):
    _, payload = completed(tmp_path)
    payload['claim_review_checks'] = claim_checks.check(payload)
    (tmp_path/'payload.json').write_text(json.dumps(payload))
    coverage_export.export_bundle(tmp_path)
    graph = json.loads((tmp_path/'answer.json').read_text())
    record = graph['claim_review_checks'][0]
    jsonschema.validate(record, api_contract.schemas()['ClaimReviewCheck'])
    run_dashboard(tmp_path, graph, """
const html=claimReviewChecksHtml(graph);
assert.match(html,/Correction required/);
assert.match(html,/data-qa-claim="C1"/);
assert.match(html,/review cues|Wording and duplicate cues/);
""")
