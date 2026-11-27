import json

from canary import coverage_runtime as engine, research_bundle, research_spec, study_editions
from test_corrections import researched

REFERENCES = {'R1': {'text_sha256': 't1', 'body_sha256': 'b1'}}


def investigation(value='4.2', denominator='40', claim='Breaking force rose by 4.2 N in Rivera 2020.'):
    result = {'id': 'SR1', 'reference_ids': ['R1'], 'study_id': 'S1', 'fields': {'units': {'text': 'N', 'passage_ids': []}},
              'outcome_result': {'estimate': {'text': value, 'passage_ids': [], 'origin': 'reported'},
                                 'precision': None,
                                 'groups': [{'group_id': 'G1', 'value': None,
                                             'denominator': {'text': denominator, 'passage_ids': [], 'origin': 'reported'}}]}}
    return {'studies': [{'id': 'S1', 'subject_count': 40}], 'study_results': [result],
            'claims': [{'id': 'C1', 'text': claim, 'study_result_ids': ['SR1']},
                       {'id': 'C2', 'text': 'Unrelated.', 'study_result_ids': []}],
            'executive_summary': ['A string paragraph.', {'text': 'Force rose.', 'claim_ids': ['C1']}]}


def test_an_edition_is_recorded_only_when_the_table_changes(tmp_path):
    first = study_editions.record(tmp_path, investigation(), REFERENCES)
    assert first['number'] == 1
    assert study_editions.record(tmp_path, dict(investigation(), claims=[]), REFERENCES) == first
    second = study_editions.record(tmp_path, investigation(denominator='38'), REFERENCES)
    editions = study_editions.history(tmp_path)
    assert second == {'number': 2, 'sha256': editions[1]['sha256']} and editions[1]['previous'] == first['sha256']
    change, = editions[1]['changes']
    assert change['fields'] == ['values.group G1 denominator']
    assert change['before']['values']['group G1 denominator'] == '40'
    assert change['after']['values']['group G1 denominator'] == '38'
    assert change['claim_ids'] == ['C1'] and change['unrevised_claim_ids'] == ['C1'] and change['summary_indexes'] == [2]
    assert editions[0]['results']['SR1']['values']['group G1 denominator'] == '40'


def test_a_claim_revised_after_the_change_is_no_longer_flagged(tmp_path):
    study_editions.record(tmp_path, investigation(), REFERENCES)
    study_editions.record(tmp_path, investigation(value='3.9'), REFERENCES)
    editions = study_editions.history(tmp_path)
    assert study_editions.stale_claims(editions, investigation(value='3.9')) == [
        {'kind': 'claim_uses_changed_value', 'claim_id': 'C1', 'study_result_id': 'SR1', 'edition': 2,
         'fields': ['values.estimate']}]
    revised = investigation(value='3.9', claim='Breaking force rose by 3.9 N in Rivera 2020.')
    assert study_editions.stale_claims(editions, revised) == []
    study_editions.record(tmp_path, dict(revised, study_results=[]), REFERENCES)
    removed = study_editions.history(tmp_path)[2]['changes'][0]
    assert removed['removed'] and removed['after'] is None


def test_unit_and_report_copy_changes_are_editions_too(tmp_path):
    study_editions.record(tmp_path, investigation(), REFERENCES)
    changed = investigation()
    changed['study_results'][0]['fields']['units']['text'] = 'kN'
    study_editions.record(tmp_path, changed, {'R1': {'text_sha256': 't2', 'body_sha256': 'b1'}})
    assert study_editions.history(tmp_path)[1]['changes'][0]['fields'] == ['documents', 'unit']


def test_research_and_correction_rounds_record_editions_and_checks_do_not(tmp_path):
    research_bundle.initialize(tmp_path, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(tmp_path)
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), minimum_papers=1, study_table_version=1))
    engine.write(tmp_path / 'investigation.json', dict(json.loads((tmp_path / 'investigation.json').read_text()), studies=[]))
    assert 'study_table_edition' not in engine.bind_payload(tmp_path)
    assert study_editions.history(tmp_path) == []
    assert engine.bind_payload(tmp_path, record_edition=True)['study_table_edition']['number'] == 1
    assert len(study_editions.history(tmp_path)) == 1


def test_the_dashboard_lists_each_edition_and_the_claims_a_change_affects(tmp_path):
    from test_coverage_evidence_ui import run_dashboard

    study_editions.record(tmp_path, investigation(), REFERENCES)
    study_editions.record(tmp_path, investigation(value='3.9'), REFERENCES)
    graph = {'study_table_editions': study_editions.history(tmp_path), 'studies': [], 'study_summary': {},
             'binding_issues': [dict(i, section='studies') for i in
                                study_editions.stale_claims(study_editions.history(tmp_path), investigation(value='3.9'))]}
    run_dashboard(tmp_path, graph, r'''
const html=studyTableHtml(graph);
assert.match(html,/Study table edition 2 · <code>/);
assert.match(html,/Editions and changes \(2\)/);
assert.match(html,/SR1 · values.estimate/);
assert.match(html,/data-qa-claim="C1">C1 · not revised</);
assert.match(html,/Summary paragraphs 2/);
assert.match(html,/Claim C1 was not revised after edition 2 changed result SR1 \(values.estimate\)\./);
''')


def test_the_export_carries_the_editions_and_the_current_one(tmp_path):
    from canary import coverage_export

    research_bundle.initialize(tmp_path, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(tmp_path)
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), study_table_version=1))
    raw = dict(json.loads((tmp_path / 'investigation.json').read_text()), studies=[])
    engine.write(tmp_path / 'investigation.json', raw)
    claim = raw['claims'][0]
    study_editions.record(tmp_path, dict(raw, study_results=[{'id': 'SR1', 'fields': {}}],
                                         claims=[dict(claim, study_result_ids=['SR1'])]), {})
    study_editions.record(tmp_path, dict(raw, claims=[dict(claim, study_result_ids=['SR1'])]), {})
    engine.write(tmp_path / 'investigation.json', dict(raw, claims=[dict(claim, study_result_ids=['SR1'])]))
    graph = coverage_export.export_bundle(tmp_path)['answer']
    assert [e['number'] for e in graph['study_table_editions']] == [1, 2]
    assert graph['study_table_edition']['number'] == 2
    assert [i['claim_id'] for i in graph['binding_issues'] if i['kind'] == 'claim_uses_changed_value'] == [claim['id']]
