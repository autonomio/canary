import json

from canary import coverage_runtime as engine, research_bundle, research_spec, study_table
from test_corrections import researched


def field(*passage_ids):
    return {'text': 'Recorded detail.', 'passage_ids': list(passage_ids)}


def study(**changes):
    record = {'id': 'S1', 'citation': 'Rivera 2020; Rivera 2022', 'subject': 'participants', 'subject_count': 40,
              'reports': [{'reference_id': 'R1', 'status': 'confirmed', 'basis': 'Same trial registration.',
                           'passage_ids': ['R1-p1']},
                          {'reference_id': 'R2', 'status': 'uncertain', 'basis': 'Same site and enrolment years.',
                           'passage_ids': ['R2-p1']}],
              'characteristics': {'design': field('R1-p1'), 'setting': None, 'population': field('R2-p1'),
                                  'sample_size': field('R1-p1'), 'follow_up': field('R2-p1'), 'funding': None},
              'groups': [{'id': 'G1', 'description': field('R1-p1'), 'sample_size': field('R1-p1')}],
              'planned_variables': [{'name': 'Age', 'value': None}], 'shared_with': []}
    record.update(changes)
    return record


PASSAGES = {'R1-p1': {'reference_id': 'R1'}, 'R2-p1': {'reference_id': 'R2'}, 'R3-p1': {'reference_id': 'R3'}}
REFERENCES = {'R1': {}, 'R2': {}, 'R3': {}}
PLAN = {'other_variables': ['Age']}
REVIEWS = [{'work_id': 'R1', 'related_study_ids': ['S1']}, {'work_id': 'R2', 'related_study_ids': ['S1']}]


def kinds(studies, reviews=REVIEWS):
    found = study_table.issues(studies, references=REFERENCES, passages=PASSAGES, reviews=reviews, plan=PLAN)
    return sorted((issue['kind'], issue.get('field') or issue.get('reference_id') or issue.get('variable')
                   or issue.get('shared_study_id')) for issue in found)


def test_a_study_with_two_reports_and_cited_characteristics_has_no_issues():
    assert kinds([study()]) == []


def test_study_passages_come_from_the_study_reports():
    characteristics = dict(study()['characteristics'], setting=field('R3-p1'), funding=field())
    assert kinds([study(characteristics=characteristics)]) == [('lacks_study_passage', 'funding'),
                                                               ('undeclared_study_report', 'setting')]


def test_reports_groups_variables_and_shared_studies_are_checked():
    record = study(groups=study()['groups'] * 2, planned_variables=[],
                   reports=[*study()['reports'], {'reference_id': 'R9', 'status': 'disputed', 'basis': 'Title only.',
                                                  'passage_ids': ['R1-p1']}],
                   shared_with=[{'study_id': 'S7', 'shares': 'datasets', 'status': 'uncertain',
                                 'basis': 'Same registry extract.', 'passage_ids': ['R1-p9']}])
    assert kinds([record]) == [('duplicate_study_group', None), ('missing_planned_variable', 'Age'),
                               ('missing_study_passage', 'shared with S7'), ('missing_study_report', 'R9'),
                               ('undeclared_study_report', 'report R9'), ('unknown_shared_study', 'S7')]


def test_every_included_report_belongs_to_the_study_its_review_names():
    reviews = [{'work_id': 'R1', 'related_study_ids': ['S2']}, {'work_id': 'R2', 'related_study_ids': ['S1']},
               {'work_id': 'R3', 'related_study_ids': []}]
    assert kinds([study()], reviews) == [('report_without_study', 'R3'), ('study_membership_mismatch', 'R1')]


def test_study_defects_are_reported_without_withholding_claims(tmp_path):
    research_bundle.initialize(tmp_path, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(tmp_path)
    run = engine.read(tmp_path / 'run.json')
    engine.write(tmp_path / 'run.json', dict(run, minimum_papers=1, study_table_version=1))
    raw = json.loads((tmp_path / 'investigation.json').read_text())
    passage = engine.read(tmp_path / 'reviewed-papers.json')[0]['passage_ids_read'][0]
    raw['studies'] = [study(id='F1', subject_count=None, planned_variables=[],
                           reports=[{'reference_id': 'W1', 'status': 'confirmed', 'basis': 'Single report.',
                                     'passage_ids': [passage]}],
                           characteristics=dict(study()['characteristics'], design=field(passage), population=None,
                                                sample_size=None, follow_up=field('W1-missing')),
                           groups=[])]
    engine.write(tmp_path / 'investigation.json', raw)
    payload = engine.bind_payload(tmp_path)
    mechanical = payload['mechanical_audit']
    assert mechanical['issues'] == ['Study table: missing study passage (study id F1, field follow_up, passage id W1-missing)',
                                    'Study table: study membership mismatch (reference id W1, review study ids [], study ids [\'F1\'])']
    assert mechanical['claim_blocking_issues'] == [] and mechanical['claim_issues'] == {}
    assert payload['studies'] == raw['studies']


def test_exported_studies_keep_their_passages_and_binding_issues(tmp_path):
    from canary import coverage_export

    research_bundle.initialize(tmp_path, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(tmp_path)
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), study_table_version=1))
    passage = engine.read(tmp_path / 'reviewed-papers.json')[0]['passage_ids_read'][0]
    raw = json.loads((tmp_path / 'investigation.json').read_text())
    raw['studies'] = [study(id='F1', planned_variables=[], groups=[],
                           reports=[{'reference_id': 'W1', 'status': 'confirmed', 'basis': 'Single report.',
                                     'passage_ids': [passage]}],
                           characteristics={name: None for name in study_table.CHARACTERISTICS} | {'design': field(passage)})]
    engine.write(tmp_path / 'investigation.json', raw)
    graph = coverage_export.export_bundle(tmp_path)['answer']
    exported, = graph['studies']
    assert exported['characteristics']['design'] == {'value': 'Recorded detail.', 'paragraph_ids': [passage]}
    assert exported['characteristics']['setting'] is None
    assert passage in {p['id'] for p in graph['passages']}
    assert [i['kind'] for i in graph['binding_issues'] if 'study' in i['kind']] == ['study_membership_mismatch']


def test_runs_started_before_the_study_table_skip_its_checks(tmp_path):
    research_bundle.initialize(tmp_path, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(tmp_path)
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), minimum_papers=1))
    engine.write(tmp_path / 'investigation.json', dict(json.loads((tmp_path / 'investigation.json').read_text()), studies=[]))
    assert engine.bind_payload(tmp_path)['mechanical_audit']['pass']
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), study_table_version=1))
    assert engine.bind_payload(tmp_path)['mechanical_audit']['issues'] == [
        'Study table: report without study (reference id W1, review study ids [])']


def test_a_group_without_a_cited_description_keeps_its_sample_size():
    import jsonschema
    from canary import coverage_protocol

    shape = coverage_protocol.investigation_schema()['properties']['studies']['items']
    record = study(groups=[{'id': 'G1', 'description': None, 'sample_size': field('R1-p1')}])
    jsonschema.Draft202012Validator(shape).validate(record)
    assert kinds([record]) == []


def test_a_subject_total_counts_each_study_once():
    first = study(subject_count=40)
    later = study(id='S2', subject_count=25, reports=[{'reference_id': 'R3', 'status': 'confirmed',
                                                      'basis': 'Separate cohort.', 'passage_ids': ['R3-p1']}])
    assert study_table.summary([first, later]) == {'studies': 2, 'reports': 3, 'subjects': [
        {'subject': 'participants', 'studies': 2, 'reasons': [], 'total': 65}]}
    shared = dict(later, shared_with=[{'study_id': 'S1', 'shares': 'participants', 'status': 'uncertain',
                                       'basis': 'Overlapping enrolment.', 'passage_ids': ['R3-p1']}])
    unknown = dict(first, subject_count=None, shared_with=[{'study_id': 'S2', 'shares': 'participants',
                                                           'status': 'confirmed', 'basis': 'Same registry.',
                                                           'passage_ids': ['R1-p1']}])
    subjects, = study_table.summary([unknown, shared])['subjects']
    assert subjects['total'] is None
    assert subjects['reasons'] == ['Study S1 does not report its number of participants.',
                                   'Studies S1 and S2 share participants, so their counts of participants may overlap.']
    linked = dict(shared, shared_with=[dict(shared['shared_with'][0], shares='datasets')])
    assert study_table.summary([first, linked])['subjects'][0]['reasons'] == [
        'Studies S1 and S2 share datasets, so their counts of participants may overlap.']
    repeated, = study_table.summary([first, first])['subjects']
    assert repeated['total'] is None and repeated['reasons'] == ['Study S1 is recorded more than once.']
    specimens = study(id='S3', subject='specimens', subject_count=12, reports=[])
    assert [s['total'] for s in study_table.summary([first, specimens])['subjects']] == [40, 12]


def test_the_study_table_is_served_with_its_summary_and_issues(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from canary import dashboard, runs

    answer = {'studies': [{'id': 'S1'}], 'study_summary': {'studies': 1, 'reports': 0, 'subjects': []},
              'binding_issues': [{'kind': 'report_without_study', 'section': 'studies'}, {'kind': 'lacks_evidence'}]}
    handler = object.__new__(dashboard.Handler)
    handler.supervisor = SimpleNamespace(root=tmp_path, registry=None)
    handler.dashboard_enabled = True
    responses = []
    handler._send = lambda *args: responses.append(args)
    monkeypatch.setattr(runs, 'get', lambda key, registry: {'key': key, 'bundle': str(tmp_path)} if key == 'review' else None)
    monkeypatch.setattr(dashboard, 'research_for_row', lambda row, supervisor: ({'answer': answer}, False))
    for path in ('/api/runs/review/studies', '/api/runs/missing/studies'):
        handler.path = path
        handler.do_GET()
    answer.pop('studies')
    handler.path = '/api/runs/review/studies'
    handler.do_GET()
    assert [status for status, _ in responses] == [200, 404, 409]
    assert responses[0][1] == {'key': 'review', 'studies': [{'id': 'S1'}], 'summary': answer['study_summary'],
                               'issues': [{'kind': 'report_without_study', 'section': 'studies'}]}


def test_the_dashboard_shows_each_study_with_its_reports_and_unknown_total(tmp_path):
    from test_coverage_evidence_ui import run_dashboard

    graph = {'passages': [{'id': 'R1-p1', 'source_id': 'R1'}],
             'studies': study_table.export([study(subject_count=None, reports=study()['reports'][:1])]),
             'study_summary': study_table.summary([study(subject_count=None)]),
             'binding_issues': [{'kind': 'report_without_study', 'section': 'studies', 'reference_id': 'R3',
                                 'review_study_ids': []}]}
    run_dashboard(tmp_path, graph, r'''
const html=studyTableHtml(graph);
assert.match(html,/Studies · 1 from 2 reports/);
assert.match(html,/Total unknown participants across 1 study/);
assert.match(html,/Study S1 does not report its number of participants/);
assert.match(html,/R1 · confirmed/);
assert.match(html,/data-qa-passage="R1-p1"/);
assert.match(html,/Included report R3 belongs to no study; its review names no study/);
assert.doesNotMatch(html,/R2-p1/);
assert.equal(studyTableHtml({}),'');
const sized=studyTableHtml({...graph,studies:[{...graph.studies[0],groups:[{id:'G1',description:null,sample_size:{value:'n = 40',paragraph_ids:['R1-p1']}}]}]});
assert.match(sized,/G1<\/p><p class="hint">Sample size<\/p><p>n = 40<\/p><div class="source-actions"><button[^>]*data-qa-passage="R1-p1"/);
assert.equal(qaGraph({answer:{studies:graph.studies}}).studies,graph.studies);
''')


VERSIONS = [{'sha256': 'V1', 'number': 1, 'outcomes': [
    {'id': 'O1', 'name': 'Breaking force', 'unit': 'N', 'time_point': '24 hours', 'effect_measure': 'mean difference'}]}]


def result(**changes):
    record = {'id': 'SR1', 'reference_ids': ['R1'], 'study_id': 'S1', 'outcome': {'id': 'O1', 'protocol_version': 'V1'},
              'fields': {'units': {'text': 'N', 'passage_ids': ['R1-p1']}},
              'outcome_result': {'time_point': field('R1-p1') | {'text': '24  Hours'},
                                 'effect_measure': field('R1-p1') | {'text': 'Mean difference'},
                                 'groups': [{'group_id': 'G1', 'value': field('R1-p1'), 'denominator': field('R1-p1')}],
                                 'estimate': field('R1-p1'), 'precision': None,
                                 'larger_value_favours': {'group_id': 'G1', 'basis': 'Higher force is stronger.',
                                                          'passage_ids': ['R1-p1']}}}
    record.update(changes)
    return record


def result_kinds(results, versions=VERSIONS):
    found = study_table.result_issues(results, [study()], versions=versions, passages=PASSAGES)
    return sorted((issue['kind'], issue.get('field') or issue.get('group_id') or issue.get('outcome_id')
                   or issue.get('protocol_version') or issue.get('study_id')) for issue in found)


def test_a_result_follows_its_study_and_the_protocol_outcome_version():
    assert result_kinds([result()]) == []


def test_a_result_that_departs_from_the_protocol_definition_is_flagged():
    recorded = dict(result()['outcome_result'], time_point=field('R1-p1') | {'text': '48 hours'})
    found = study_table.result_issues([result(outcome_result=recorded)], [study()], versions=VERSIONS, passages=PASSAGES)
    assert found == [{'kind': 'outcome_definition_mismatch', 'study_result_id': 'SR1', 'study_id': 'S1',
                      'field': 'time_point', 'protocol_value': '24 hours', 'recorded_value': '48 hours'}]
    assert result_kinds([result(fields={'units': {'text': 'mN', 'passage_ids': ['R1-p1']}})]) == [
        ('outcome_definition_mismatch', 'unit')]
    assert result_kinds([result(outcome={'id': 'O9', 'protocol_version': 'V1'})]) == [('unknown_protocol_outcome', 'O9')]
    assert result_kinds([result(outcome={'id': 'O1', 'protocol_version': 'V0'})]) == [('unknown_protocol_version', 'V0')]
    assert result_kinds([result(outcome_result=None)]) == [('missing_outcome_result', 'O1')]


def test_a_result_stays_within_its_study_groups_and_reports():
    recorded = dict(result()['outcome_result'], groups=[{'group_id': 'G4', 'value': field('R3-p1'), 'denominator': None}])
    assert result_kinds([result(reference_ids=['R1', 'R3'], outcome_result=recorded)]) == [
        ('result_outside_study', 'S1'), ('unknown_result_group', 'G4')]
    assert result_kinds([result(study_id='S8')]) == [('unknown_result_study', 'S8')]
    unplanned = result(outcome=None, outcome_result=None)
    assert result_kinds([unplanned]) == []


def test_the_dashboard_shows_an_outcome_result_with_its_protocol_version(tmp_path):
    from test_coverage_evidence_ui import run_dashboard

    exported = dict(result(), fields={}, **study_table.export_result(result()))
    graph = {'passages': [{'id': 'R1-p1', 'source_id': 'R1'}], 'study_results': [exported],
             'protocol_outcomes': [dict(VERSIONS[0]['outcomes'][0], protocol_version='V1', protocol_version_number=1)],
             'binding_issues': [{'kind': 'outcome_definition_mismatch', 'section': 'studies', 'study_result_id': 'SR1',
                                 'field': 'unit', 'protocol_value': 'N', 'recorded_value': 'kN'}]}
    run_dashboard(tmp_path, graph, r'''
const html=outcomeResultHtml(graph,graph.study_results[0]);
assert.match(html,/Planned outcome<\/dt><dd><p>Breaking force<\/p><p class="hint">Protocol version V1 · unit: N · time point: 24 hours · effect measure: mean difference/);
assert.match(html,/Result SR1 unit is kN; the protocol defines N\./);
assert.match(html,/G1 denominator/);
assert.match(html,/A larger value favours<\/dt><dd><p>G1<\/p>/);
assert.match(html,/Precision<\/dt><dd><span class="muted">Not reported/);
assert.equal(outcomeResultHtml(graph,{id:'X'}),'');
''')


def test_result_checks_read_the_saved_protocol(tmp_path):
    from canary import review_protocol

    research_bundle.initialize(tmp_path, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(tmp_path)
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), minimum_papers=1, study_table_version=1))
    plan = engine.read(tmp_path / 'question-plan.json')
    review_protocol.save_plan(tmp_path, dict(plan, outcomes=[{'name': 'Breaking force', 'unit': 'N', 'time_point': None,
                                                              'effect_measure': None}]))
    version = review_protocol.versions(tmp_path / review_protocol.FILE)[-1]
    passage = engine.read(tmp_path / 'reviewed-papers.json')[0]['passage_ids_read'][0]
    raw = json.loads((tmp_path / 'investigation.json').read_text())
    raw['studies'] = [study(id='F1', planned_variables=[], groups=[],
                           reports=[{'reference_id': 'W1', 'status': 'confirmed', 'basis': 'Single report.',
                                     'passage_ids': [passage]}],
                           characteristics={name: None for name in study_table.CHARACTERISTICS})]
    engine.write(tmp_path / 'reviewed-papers.json', [dict(engine.read(tmp_path / 'reviewed-papers.json')[0],
                                                          related_study_ids=['F1'])])
    raw['study_results'] = [{'id': 'SR1', 'reference_ids': ['W1'], 'result_type': 'measured', 'version_or_cohort_note': '',
                             'fields': {'units': {'text': 'kN', 'passage_ids': [passage]}}, 'study_id': 'F1',
                             'outcome': {'id': version['outcomes'][0]['id'], 'protocol_version': version['sha256']},
                             'outcome_result': {'time_point': None, 'effect_measure': None, 'groups': [],
                                                'estimate': None, 'precision': None, 'larger_value_favours': None}}]
    engine.write(tmp_path / 'investigation.json', raw)
    assert engine.bind_payload(tmp_path)['mechanical_audit']['issues'] == [
        'Study table: outcome definition mismatch (study result id SR1, study id F1, field unit, protocol value N, '
        'recorded value kN)']


def test_result_passages_come_from_the_result_references():
    recorded = dict(result()['outcome_result'], time_point=field('R2-p1') | {'text': '24 hours'})
    assert result_kinds([result(outcome_result=recorded)]) == [('undeclared_result_reference', 'time_point')]


def test_an_unreadable_protocol_leaves_outcome_versions_unchecked():
    assert result_kinds([result(outcome={'id': 'O1', 'protocol_version': 'V0'})], versions=None) == []
    assert result_kinds([result(outcome_result=None)], versions=None) == [('missing_outcome_result', 'O1')]


def test_a_result_citing_an_earlier_protocol_version_is_listed():
    amended = [*VERSIONS, {'sha256': 'V2', 'number': 2, 'outcomes': VERSIONS[0]['outcomes']}]
    found = study_table.result_issues([result()], [study()], versions=amended, passages=PASSAGES)
    assert found == [{'kind': 'superseded_protocol_version', 'study_result_id': 'SR1', 'study_id': 'S1',
                      'protocol_version': 'V1', 'version_number': 1, 'latest_version_number': 2}]


def test_results_and_issues_naming_an_unrecorded_study_appear_above_the_table(tmp_path):
    from test_coverage_evidence_ui import run_dashboard

    orphan = dict(result(study_id='S8'), fields={})
    issues = [dict(i, section='studies') for i in study_table.result_issues([orphan], [study()], versions=VERSIONS,
                                                                           passages=PASSAGES)]
    graph = {'passages': [], 'studies': study_table.export([study()]), 'study_summary': study_table.summary([study()]),
             'study_results': [orphan], 'binding_issues': issues}
    run_dashboard(tmp_path, graph, r'''
const html=studyTableHtml(graph);
const [above]=html.split('<div class="table-wrap"');
assert.match(above,/Result SR1 names study S8, which is not recorded\./);
assert.match(above,/Results naming an unrecorded study<\/p><div class="source-actions"><button[^>]*data-qa-study="SR1"/);
assert.doesNotMatch(html,/names group/);
''')
