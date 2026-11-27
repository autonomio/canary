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
