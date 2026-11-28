import json
from types import SimpleNamespace

import pytest

from canary import analysis_import, study_editions
from test_study_table import VERSIONS, result


def measured(identifier, study, value, **changes):
    base = result(id=identifier, study_id=study)
    return dict(base, outcome_result=dict(base['outcome_result'], estimate={'text': value, 'passage_ids': ['R1-p1']}), **changes)


RESULTS = [measured('SR1', 'S1', '2'), measured('SR2', 'S2', '4')]


def analysis(**changes):
    data = {'synthesis_id': 'Y1', 'outcome': 'Breaking force', 'unit': 'N', 'time_point': '24 hours',
            'effect_measure': 'Mean difference', 'software': 'metafor', 'software_version': '4.6-0',
            'inputs': [{'study_id': 'S1', 'study_result_id': 'SR1', 'estimate': 2, 'standard_error': 1},
                       {'study_id': 'S2', 'study_result_id': 'SR2', 'estimate': 4, 'standard_error': 1}],
            'analyses': [{'model': 'inverse_variance_fixed', 'estimate': 3.0, 'lower': 1.61, 'upper': 4.39,
                          'q': 2.0, 'i2': 50.0, 'tau2': None},
                         {'model': 'dersimonian_laird_random', 'estimate': 3.0, 'lower': 1.04, 'upper': 4.96,
                          'q': 2.0, 'i2': 50.0, 'tau2': 1.0}]}
    data.update(changes)
    return data


def investigation(results=RESULTS):
    return {'claims': [], 'studies': [], 'study_results': results, 'syntheses': [{'id': 'Y1', 'study_result_ids': ['SR1', 'SR2']}]}


def table(results=RESULTS):
    return [{'number': 1, 'sha256': 'e1', 'results': study_editions.contents(investigation(results), {})['results']}]


def checked(data, results=RESULTS):
    return analysis_import.check(data, table(results), VERSIONS[-1]['outcomes'])


def test_an_imported_reference_analysis_matches_the_table_and_is_reproduced():
    found = checked(analysis())
    assert found['inputs_match'] == {'status': 'matched', 'mismatches': [], 'edition': 1}
    assert [(c['model'], c['status']) for c in found['reproduction']] == [
        ('inverse_variance_fixed', 'reproduced'), ('dersimonian_laird_random', 'reproduced')]
    assert found['stale'] == []


def test_a_second_report_of_a_study_fails_the_input_check_but_not_the_estimate():
    second = {'study_id': 'S1', 'study_result_id': 'SR3', 'estimate': 2, 'standard_error': 1}
    found = checked(analysis(inputs=[*analysis()['inputs'], second]), RESULTS + [measured('SR3', 'S1', '2')])
    assert [(m['kind'], m['study_id']) for m in found['inputs_match']['mismatches']] == [('duplicate_import_study', 'S1')]
    assert found['inputs_match']['status'] == 'mismatched'
    assert [c['status'] for c in found['reproduction']] == ['reproduced', 'reproduced']


@pytest.mark.parametrize('field, value', [('unit', 'kN'), ('time_point', '6 months'), ('effect_measure', 'Ratio of means'),
                                          ('outcome', 'Elongation')])
def test_a_changed_label_withholds_the_input_match_while_numbers_still_reproduce(field, value):
    found = checked(analysis(**{field: value}))
    assert found['inputs_match']['status'] == 'mismatched'
    assert {(m['field'], m['imported']) for m in found['inputs_match']['mismatches']} == {(field, value)}
    assert [c['status'] for c in found['reproduction']] == ['reproduced', 'reproduced']


def test_unknown_results_other_studies_and_other_values_are_mismatches():
    inputs = [{'study_id': 'S9', 'study_result_id': 'SR1', 'estimate': 2.5, 'standard_error': 1},
              {'study_id': 'S2', 'study_result_id': 'SR9', 'estimate': 4, 'standard_error': 1}]
    found = checked(analysis(inputs=inputs))
    assert sorted((m['kind'], m.get('field')) for m in found['inputs_match']['mismatches']) == [
        ('import_input_mismatch', 'estimate'), ('import_input_mismatch', 'study_id'), ('unknown_import_result', None)]
    assert [c['status'] for c in found['reproduction']] == ['differs', 'differs']


def test_the_file_is_kept_unchanged_and_goes_stale_when_a_contributing_value_changes(tmp_path):
    references = {'R1': {'text_sha256': 't1', 'body_sha256': 'b1'}}
    study_editions.record(tmp_path, investigation(), references)
    content = json.dumps(analysis(), indent=1)
    record, created = analysis_import.add(tmp_path, {'Y1'}, 'pooled.json', content)
    assert created and record['id'] == 'A1' and record['edition']['number'] == 1
    assert (tmp_path / analysis_import.FOLDER / f"{record['file_sha256']}.json").read_bytes() == content.encode()
    assert analysis_import.add(tmp_path, {'Y1'}, 'again.json', content) == (record, False)
    changed = [measured('SR1', 'S1', '2.5'), RESULTS[1]]
    study_editions.record(tmp_path, investigation(changed), references)
    editions = study_editions.history(tmp_path)
    assert editions[0]['results']['SR1']['estimate']['text'] == '2'
    graph = {'syntheses': [], 'study_table_editions': editions, 'protocol_outcomes': VERSIONS[-1]['outcomes'],
             'binding_issues': [{'kind': 'import_input_mismatch', 'import_id': 'A0'}, {'kind': 'lacks_evidence'}]}
    exported, = analysis_import.attach(tmp_path, graph)['analysis_imports']
    assert exported['file_sha256'] == record['file_sha256'] and exported['inputs_match']['edition'] == 2
    assert exported['stale'] == [{'study_result_id': 'SR1', 'fields': ['estimate'], 'imported_edition': 1, 'current_edition': 2}]
    assert [(m['field'], m['table']) for m in exported['inputs_match']['mismatches']] == [('estimate', '2.5')]
    assert [(i['kind'], i.get('import_id')) for i in graph['binding_issues']] == [
        ('lacks_evidence', None), ('import_input_mismatch', 'A1'), ('import_uses_changed_value', 'A1')]


def test_malformed_files_and_unknown_syntheses_are_refused(tmp_path):
    with pytest.raises(ValueError, match='not JSON'):
        analysis_import.parse('{')
    with pytest.raises(ValueError, match='inputs/0'):
        analysis_import.parse(json.dumps(analysis(inputs=[{'study_id': 'S1', 'study_result_id': 'SR1', 'estimate': '2',
                                                           'standard_error': 1}])))
    with pytest.raises(LookupError):
        analysis_import.add(tmp_path, {'Y2'}, 'pooled.json', json.dumps(analysis()))
    assert analysis_import.history(tmp_path) == []


def test_the_analyses_api_imports_and_lists_files(tmp_path, monkeypatch):
    from canary import dashboard, runs

    bundle = tmp_path / 'container-out' / 'review'
    bundle.mkdir(parents=True)
    rows = {'review': {'key': 'review', 'bundle': str(bundle), 'run_spec_path': 'spec.yaml'},
            'adopted': {'key': 'adopted', 'bundle': str(tmp_path), 'adopted': True}}
    answer = {'syntheses': [{'id': 'Y1'}], 'analysis_imports': [], 'binding_issues': [{'kind': 'import_uses_changed_value', 'import_id': 'A1'},
                                                                                      {'kind': 'lacks_evidence'}]}
    handler = object.__new__(dashboard.Handler)
    handler.supervisor = SimpleNamespace(root=tmp_path, registry=None)
    handler.dashboard_enabled = True
    responses = []
    handler._send = lambda *args: responses.append(args)
    handler._local_origin = lambda: True
    monkeypatch.setattr(runs, 'get', lambda key, registry: rows.get(key))
    monkeypatch.setattr(dashboard, 'research_for_row', lambda row, supervisor: ({'answer': answer}, False))
    content = json.dumps(analysis())
    for path, body in (('/api/runs/review/analyses', {'file_name': '../pooled.json', 'content': content}),
                       ('/api/runs/review/analyses', {'file_name': 'pooled.json', 'content': content}),
                       ('/api/runs/review/analyses', {'content': content}),
                       ('/api/runs/review/analyses', {'file_name': 'other.json', 'content': json.dumps(analysis(synthesis_id='Y9'))}),
                       ('/api/runs/adopted/analyses', {'file_name': 'pooled.json', 'content': content}),
                       ('/api/runs/missing/analyses', {'file_name': 'pooled.json', 'content': content})):
        handler.path = path
        handler._read_json = lambda body=body: body
        handler.do_POST()
    handler.path = '/api/runs/review/analyses'
    handler.do_GET()
    assert [status for status, *_ in responses] == [201, 200, 400, 409, 409, 404, 200]
    assert responses[0][1]['file_name'] == 'pooled.json' and (bundle / analysis_import.FILE).is_file()
    assert responses[3][1]['code'] == 'unknown_synthesis' and responses[4][1]['code'] == 'analysis_import_unavailable'
    assert responses[6][1] == {'key': 'review', 'imports': [], 'issues': [{'kind': 'import_uses_changed_value', 'import_id': 'A1'}]}


def test_imports_reach_the_export_with_their_checks(tmp_path):
    from canary import coverage_export, coverage_runtime as engine, research_bundle, research_spec
    from test_corrections import researched

    research_bundle.initialize(tmp_path, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(tmp_path)
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), study_table_version=1, synthesis_version=1))
    raw = json.loads((tmp_path / 'investigation.json').read_text())
    raw['syntheses'] = [{'id': 'Y1', 'study_result_ids': []}]
    engine.write(tmp_path / 'investigation.json', raw)
    engine.bind_payload(tmp_path)
    analysis_import.add(tmp_path, {'Y1'}, 'pooled.json', json.dumps(analysis()))
    graph = coverage_export.export_bundle(tmp_path)['answer']
    imported, = graph['analysis_imports']
    assert imported['id'] == 'A1' and imported['inputs_match']['status'] == 'mismatched'
    assert [c['status'] for c in imported['reproduction']] == ['reproduced', 'reproduced']
    assert {i['kind'] for i in graph['binding_issues'] if i.get('import_id') == 'A1'} == {'unknown_import_result'}


def test_the_dashboard_shows_each_import_under_its_synthesis(tmp_path):
    from test_coverage_evidence_ui import run_dashboard
    from test_synthesis import synthesis_record

    record = dict(analysis(), id='A1', file_name='pooled.json', file_sha256='ab' * 32, edition={'number': 2, 'sha256': 'e'},
                  **checked(analysis(unit='kN')))
    record['stale'] = [{'study_result_id': 'SR1', 'fields': ['estimate'], 'imported_edition': 1, 'current_edition': 2}]
    graph = {'syntheses': [dict(synthesis_record(), study_ids=['S1', 'S2'])], 'analysis_imports': [record], 'binding_issues': []}
    run_dashboard(tmp_path, graph, r'''
const html=synthesesHtml(graph);
assert.match(html,/<dt>Imported analyses<\/dt><dd><p>pooled\.json · <code>abababababab<\/code> · metafor 4\.6-0 · study table version 2<\/p>/);
assert.match(html,/Inputs do not match the study table \(2\) · Reproduced by Canary · stale: SR1 changed/);
assert.match(html,/DerSimonian–Laird random effects: 3 \[1\.04, 4\.96\] · Q 2 · I² 50% · τ² 1/);
assert.equal(synthesisIssueText({kind:'import_input_mismatch',import_id:'A1',field:'time_point',imported:'6 months',study_result_id:'SR1',table:'24 hours'}),'Import A1 gives time point “6 months” for SR1; the study table has “24 hours”.');
assert.equal(synthesisIssueText({kind:'duplicate_import_study',import_id:'A1',study_id:'S1',first:'SR1',study_result_id:'SR3'}),'Import A1 uses study S1 twice (SR1 and SR3).');
''')


def test_an_import_into_a_finished_run_is_checked_when_read(tmp_path, monkeypatch):
    from canary import coverage_export, coverage_runtime as engine, dashboard, research_bundle, research_spec, runs
    from test_corrections import researched

    bundle = tmp_path / 'container-out' / 'review'
    bundle.mkdir(parents=True)
    research_bundle.initialize(bundle, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(bundle)
    engine.write(bundle / 'run.json', dict(engine.read(bundle / 'run.json'), study_table_version=1, synthesis_version=1))
    raw = json.loads((bundle / 'investigation.json').read_text())
    raw['syntheses'] = [{'id': 'Y1', 'study_result_ids': []}]
    engine.write(bundle / 'investigation.json', raw)
    engine.bind_payload(bundle)
    coverage_export.export_bundle(bundle)
    answer = (bundle / 'answer.json').read_bytes()
    row = {'key': 'review', 'bundle': str(bundle), 'run_spec_path': 'spec.yaml'}
    handler = object.__new__(dashboard.Handler)
    handler.supervisor = SimpleNamespace(root=tmp_path, registry=None)
    handler.dashboard_enabled = True
    responses = []
    handler._send = lambda *args: responses.append(args)
    handler._local_origin = lambda: True
    handler._read_json = lambda: {'file_name': 'pooled.json', 'content': json.dumps(analysis())}
    monkeypatch.setattr(runs, 'get', lambda key, registry: row if key == 'review' else None)
    handler.path = '/api/runs/review/analyses'
    handler.do_POST()
    handler.do_GET()
    (status, created), (_, listed) = responses[0][:2], responses[1][:2]
    assert status == 201 and created['inputs_match']['status'] == 'mismatched'
    assert [c['status'] for c in created['reproduction']] == ['reproduced', 'reproduced'] and created['stale'] == []
    imported, = listed['imports']
    assert imported['id'] == 'A1' and imported['inputs_match'] == created['inputs_match']
    assert {i['kind'] for i in listed['issues']} == {'unknown_import_result'}
    assert (bundle / 'answer.json').read_bytes() == answer


def test_reports_of_one_study_with_different_values_stop_the_rerun():
    second = {'study_id': 'S1', 'study_result_id': 'SR3', 'estimate': 9, 'standard_error': 1}
    found = checked(analysis(inputs=[*analysis()['inputs'], second]), RESULTS + [measured('SR3', 'S1', '9')])
    assert [m['kind'] for m in found['inputs_match']['mismatches']] == ['duplicate_import_study']
    assert [c['status'] for c in found['reproduction']] == ['not_recomputed', 'not_recomputed']
