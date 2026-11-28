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
    assert change['fields'] == ['group G1 denominator'] and not change['added']
    assert change['before']['group G1 denominator']['text'] == '40'
    assert change['after']['group G1 denominator']['text'] == '38'
    assert change['claim_ids'] == ['C1'] and change['unrevised_claim_ids'] == ['C1'] and change['summary_indexes'] == [2]
    assert editions[0]['results']['SR1']['group G1 denominator']['text'] == '40'


def test_a_claim_revised_after_the_change_is_no_longer_flagged(tmp_path):
    study_editions.record(tmp_path, investigation(), REFERENCES)
    study_editions.record(tmp_path, investigation(value='3.9'), REFERENCES)
    editions = study_editions.history(tmp_path)
    assert study_editions.stale_claims(editions, investigation(value='3.9')) == [
        {'kind': 'claim_uses_changed_value', 'claim_id': 'C1', 'study_result_id': 'SR1', 'edition': 2,
         'fields': ['estimate'], 'summary_indexes': [2]}]
    revised = investigation(value='3.9', claim='Breaking force rose by 3.9 N in Rivera 2020.')
    assert study_editions.stale_claims(editions, revised) == []
    study_editions.record(tmp_path, dict(revised, study_results=[]), REFERENCES)
    removed = study_editions.history(tmp_path)[2]['changes'][0]
    assert removed['removed'] and removed['after'] is None


def test_a_claim_revised_before_the_change_is_still_flagged(tmp_path):
    study_editions.record(tmp_path, investigation(), REFERENCES)
    revised = 'Breaking force rose in Rivera 2020.'
    study_editions.record(tmp_path, investigation(claim=revised), REFERENCES)
    assert len(study_editions.history(tmp_path)) == 1
    study_editions.record(tmp_path, investigation(value='3.9', claim=revised), REFERENCES)
    editions = study_editions.history(tmp_path)
    assert editions[1]['changes'][0]['unrevised_claim_ids'] == ['C1']
    assert [i['claim_id'] for i in study_editions.stale_claims(editions, investigation(value='3.9', claim=revised))] == ['C1']


def test_a_claim_removed_before_the_change_is_not_listed_as_using_it(tmp_path):
    study_editions.record(tmp_path, investigation(), REFERENCES)
    study_editions.record(tmp_path, dict(investigation(), claims=[]), REFERENCES)
    assert study_editions.history(tmp_path)[0]['claims']['C1']['withdrawn'] is True
    study_editions.record(tmp_path, dict(investigation(value='3.9'), claims=[]), REFERENCES)
    change, = study_editions.history(tmp_path)[1]['changes']
    assert change['claim_ids'] == [] and change['unrevised_claim_ids'] == []


def test_a_claim_that_moved_to_another_result_keeps_its_earlier_dependency(tmp_path):
    first = investigation()
    first['study_results'].append(dict(first['study_results'][0], id='SR2'))
    study_editions.record(tmp_path, first, REFERENCES)
    moved = investigation(value='3.9')
    moved['study_results'].append(first['study_results'][1])
    moved['claims'][0]['study_result_ids'] = ['SR2']
    study_editions.record(tmp_path, moved, REFERENCES)
    editions = study_editions.history(tmp_path)
    change, = editions[1]['changes']
    assert change['study_result_id'] == 'SR1' and change['claim_ids'] == ['C1'] and change['unrevised_claim_ids'] == []
    assert study_editions.stale_claims(editions, moved) == []


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
assert.match(html,/<div class="study-records"><\/div><div class="study-history"><details data-detail="study-editions"><summary>Editions and changes · 2 versions, last changed \d{4}-\d\d-\d\d \d\d:\d\d UTC<\/summary>/);
assert.match(html,/<h4>Version 2 · <code>/);
assert.match(studyTableHtml({...graph,study_table_editions:graph.study_table_editions.slice(0,1)}),/<p class="hint">Saved \d{4}-\d\d-\d\d \d\d:\d\d UTC, unchanged since<\/p>/);
assert.doesNotMatch(html,/study-overview/);
assert.match(html,/Claims not revised after a result they use changed \(1\)<\/p><ul><li>Claim C1 was not revised/);
assert.match(html,/<\/code> <span class="hint">\d{4}-\d\d-\d\d \d\d:\d\d UTC<\/span>/);
assert.match(html,/SR1 · estimate/);
assert.match(html,/data-qa-claim="C1">C1 · not revised</);
assert.match(html,/<span>estimate<\/span><span><del class="edition-del">4.2<\/del><\/span><span><ins class="edition-ins">3.9<\/ins><\/span>/);
assert.match(html,/Cited in answer paragraph 2/);
const change={study_result_id:'SR1',fields:['availability','outcome'],claim_ids:[],
  before:{availability:{status:'reported',basis:'Table 2.'},outcome:{id:'a'.repeat(64),protocol_version:'b'.repeat(64)}},
  after:{availability:{status:'measured_not_reported',basis:'Methods only.'},outcome:null}};
const objects=studyTableHtml({...graph,study_table_editions:[graph.study_table_editions[0],{...graph.study_table_editions[1],changes:[change]}]});
assert.match(objects,/<span>availability<\/span><span><del class="edition-del">reported — Table 2<\/del>\.<\/span><span><ins class="edition-ins">measured not reported — Methods only<\/ins>\.<\/span>/);
assert.match(objects,/<span>outcome<\/span><span><del class="edition-del">id aaaaaaaaaaaa · protocol version bbbbbbbbbbbb<\/del><\/span><span><ins class="edition-ins">none<\/ins><\/span>/);
assert.match(objects,/No claim used this result\./);
const typo={study_result_id:'SR1',fields:['outcome'],claim_ids:[],before:{outcome:{id:'a'.repeat(20)+'c'.repeat(44)}},after:{outcome:{id:'a'.repeat(64)}}};
const formula={study_result_id:'SR1',fields:['estimate'],claim_ids:[],
  before:{estimate:{text:'0.12',origin:'calculated',calculation:{formula:'mean * 9.81',inputs:[{name:'mean',value:'12.5',unit:'kgf'}],assumptions:['g']},documents:['c'.repeat(64)]}},
  after:{estimate:{text:'0.12',origin:'calculated',calculation:{formula:'mean * 9.80',inputs:[{name:'mean',value:'12.5',unit:'kgf'}],assumptions:['g']},documents:['c'.repeat(64)]}}};
const shownFormula=studyTableHtml({...graph,study_table_editions:[graph.study_table_editions[0],{...graph.study_table_editions[1],changes:[formula]}]});
assert.match(shownFormula,/<span>0\.12 \(calculated\) · formula mean \* 9\.8<del class="edition-del">1<\/del> · inputs mean = 12\.5 kgf · assumptions g · read from c{64}<\/span><span>0\.12 \(calculated\) · formula mean \* 9\.8<ins class="edition-ins">0<\/ins>/);
const both={...formula,before:{estimate:{...formula.before.estimate,text:'0.13'}}};
const shownBoth=studyTableHtml({...graph,study_table_editions:[graph.study_table_editions[0],{...graph.study_table_editions[1],changes:[both]}]});
assert.match(shownBoth,/<span>0\.1<del class="edition-del">3 \(calculated\) · formula mean \* 9\.81<\/del> · inputs/);
assert.match(shownBoth,/<span>0\.1<ins class="edition-ins">2 \(calculated\) · formula mean \* 9\.80<\/ins> · inputs/);
const valueOnly={...formula,before:{estimate:{...formula.after.estimate,text:'0.13'}}};
assert.match(studyTableHtml({...graph,study_table_editions:[graph.study_table_editions[0],{...graph.study_table_editions[1],changes:[valueOnly]}]}),/<span>0\.1<del class="edition-del">3<\/del> \(calculated\)<\/span>/);
const shares={study_id:'S1',fields:['shared with S2','reports'],
  before:{'shared with S2':{shares:'specimens',status:'uncertain',basis:'Same lab.'},reports:[{reference_id:'R1',status:'confirmed',basis:'One report.'}]},
  after:{'shared with S2':{shares:'datasets',status:'uncertain',basis:'Same lab.'},reports:[{reference_id:'R2',status:'confirmed',basis:'One report.'}]}};
const sharedHtml=studyTableHtml({...graph,study_table_editions:[graph.study_table_editions[0],{...graph.study_table_editions[1],changes:[shares]}]});
assert.match(sharedHtml,/<span>shares <del class="edition-del">specimen<\/del>s · uncertain — Same lab\.<\/span><span>shares <ins class="edition-ins">dataset<\/ins>s · uncertain — Same lab\.<\/span>/);
assert.match(sharedHtml,/<span>reference id R<del class="edition-del">1<\/del> · confirmed — One report\.<\/span>/);
const group={study_id:'S1',fields:['group G1 description'],
  before:{'group G1 description':{text:'Grid infill.',origin:null,calculation:null,documents:['a'.repeat(64)],passages:['R1-p1']}},
  after:{'group G1 description':{text:'Grid infill, 20%.',origin:null,calculation:null,documents:['b'.repeat(64)],passages:['R1-p1']}}};
const groupHtml=studyTableHtml({...graph,study_table_editions:[graph.study_table_editions[0],{...graph.study_table_editions[1],changes:[group]}]});
assert.match(groupHtml,/<span>Grid infill<del class="edition-del">\. · read from a{64}<\/del> · passages R1-p1<\/span>/);
assert.match(groupHtml,/<span>Grid infill<ins class="edition-ins">, 20%\. · read from b{64}<\/ins> · passages R1-p1<\/span>/);
const hidden=studyTableHtml({...graph,study_table_editions:[graph.study_table_editions[0],{...graph.study_table_editions[1],changes:[typo]}]});
assert.match(hidden,new RegExp(`<span>outcome</span><span>id ${'a'.repeat(20)}<del class="edition-del">${'c'.repeat(44)}</del></span><span>id ${'a'.repeat(20)}<ins class="edition-ins">${'a'.repeat(44)}</ins></span>`));
assert.match(studyTableHtml({...graph,study_table_editions:[graph.study_table_editions[0],{...graph.study_table_editions[1],changes:[{...typo,before:{outcome:{id:'ab12'.repeat(16)}},after:{outcome:{id:'ab12'.repeat(8)+'ab'+'ab12'.repeat(8)}}}]}]}),/<span>outcome<\/span><span>id [0-9a-f]{64}<\/span><span>id [0-9a-f]+<ins class="edition-ins">[0-9a-f]{2}<\/ins>[0-9a-f]+<\/span>/);
assert.doesNotMatch(html,/Before:/);
assert.match(html,/Claim C1 was not revised after version 2 of the study table changed result SR1 \(estimate\)\. Answer paragraph 2 cites it\./);
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


def test_qualitative_availability_and_calculation_changes_are_editions(tmp_path):
    study_editions.record(tmp_path, investigation(), REFERENCES)
    for change in ({'qualitative': {'text': 'Fewer fractures.', 'passage_ids': []}},
                   {'availability': {'status': 'unclear', 'basis': 'Two reports disagree.', 'passage_ids': []}},
                   {'estimate': {'text': '4.2', 'passage_ids': [], 'origin': 'calculated', 'calculation': {
                       'inputs': [{'name': 'mean', 'value': '4.2', 'unit': 'N', 'passage_ids': []}],
                       'formula': 'mean', 'assumptions': []}}}):
        changed = investigation()
        changed['study_results'][0]['outcome_result'].update(change)
        study_editions.record(tmp_path, changed, REFERENCES)
        assert study_editions.history(tmp_path)[-1]['changes'][0]['fields'] == sorted(change)
        study_editions.record(tmp_path, investigation(), REFERENCES)


def test_added_results_and_study_counts_are_listed(tmp_path):
    study_editions.record(tmp_path, dict(investigation(), study_results=[]), REFERENCES)
    study_editions.record(tmp_path, dict(investigation(), studies=[{'id': 'S1', 'subject_count': 38}]), REFERENCES)
    added, counted = study_editions.history(tmp_path)[1]['changes']
    assert added['study_result_id'] == 'SR1' and added['added'] and added['claim_ids'] == ['C1']
    assert counted['fields'] == ['subject_count'] and not counted['added'] and not counted['removed']
    assert (counted['before']['subject_count'], counted['after']['subject_count']) == (40, 38)


def test_a_change_to_any_study_detail_is_a_recoverable_edition(tmp_path):
    funded = {'text': 'Public grant.', 'passage_ids': ['R1-p1']}
    study = {'id': 'S1', 'citation': 'Rivera 2020', 'subject': 'specimens', 'subject_count': 40,
             'reports': [{'reference_id': 'R1', 'status': 'confirmed', 'basis': 'Same specimens.', 'passage_ids': []}],
             'characteristics': {'funding': funded, 'design': None},
             'groups': [{'id': 'G1', 'description': {'text': 'Grid infill.', 'passage_ids': []}, 'sample_size': None}],
             'planned_variables': [], 'shared_with': []}
    study_editions.record(tmp_path, dict(investigation(), studies=[study]), REFERENCES, {'R1-p1': 'R1'})
    changed = dict(study, characteristics={'funding': {'text': 'Industry grant.', 'passage_ids': ['R1-p1']}, 'design': None},
                   reports=[dict(study['reports'][0], status='uncertain')],
                   shared_with=[{'study_id': 'S2', 'shares': 'specimens', 'status': 'uncertain', 'basis': 'Same lab.', 'passage_ids': []}])
    study_editions.record(tmp_path, dict(investigation(), studies=[changed]), REFERENCES, {'R1-p1': 'R1'})
    first, second = study_editions.history(tmp_path)
    change, = second['changes']
    assert change['fields'] == ['funding', 'reports', 'shared with S2']
    assert first['studies']['S1']['funding']['text'] == 'Public grant.'
    assert first['studies']['S1']['funding']['documents'] == ['t1']
    assert first['studies']['S1']['reports'] == [{'reference_id': 'R1', 'status': 'confirmed', 'basis': 'Same specimens.',
                                                  'passages': []}]
    assert first['studies']['S1']['group G1 description']['text'] == 'Grid infill.'
    assert second['studies']['S1']['shared with S2'] == {'shares': 'specimens', 'status': 'uncertain', 'basis': 'Same lab.',
                                                         'passages': []}


def test_a_new_citation_or_a_second_link_is_kept_without_flagging_claims(tmp_path):
    study = {'id': 'S1', 'reports': [{'reference_id': 'R1', 'status': 'confirmed', 'basis': 'Same.', 'passage_ids': ['R1-p1']}],
             'shared_with': [{'study_id': 'S2', 'shares': 'specimens', 'status': 'uncertain', 'basis': 'Lab.', 'passage_ids': []},
                             {'study_id': 'S2', 'shares': 'datasets', 'status': 'confirmed', 'basis': 'Data.', 'passage_ids': []}]}
    study_editions.record(tmp_path, dict(investigation(), studies=[study]), REFERENCES)
    recorded = study_editions.history(tmp_path)[0]['studies']['S1']
    assert recorded['shared with S2']['shares'] == 'specimens' and recorded['shared with S2 (2)']['shares'] == 'datasets'
    moved = dict(study, reports=[dict(study['reports'][0], passage_ids=['R1-p2'])])
    study_editions.record(tmp_path, dict(investigation(), studies=[moved]), REFERENCES)
    assert study_editions.history(tmp_path)[1]['changes'][0]['fields'] == ['reports']
    recited = investigation()
    recited['study_results'][0]['outcome_result']['estimate']['passage_ids'] = ['R1-p9']
    study_editions.record(tmp_path, dict(recited, studies=[moved]), REFERENCES)
    editions = study_editions.history(tmp_path)
    change, = editions[2]['changes']
    assert change['fields'] == ['estimate'] and change['passages_only'] and change['unrevised_claim_ids'] == []
    assert study_editions.stale_claims(editions, dict(recited, studies=[moved])) == []


def test_an_unrevised_claim_is_in_the_mechanical_audit(tmp_path):
    research_bundle.initialize(tmp_path, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(tmp_path)
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), minimum_papers=1, study_table_version=1))
    raw = dict(json.loads((tmp_path / 'investigation.json').read_text()), studies=[])
    claim = dict(raw['claims'][0], study_result_ids=['SR1'])
    results = [{'id': 'SR1', 'reference_ids': [], 'fields': {'value': {'text': '4', 'passage_ids': []}}}]
    engine.write(tmp_path / 'investigation.json', dict(raw, claims=[claim], study_results=results))
    engine.bind_payload(tmp_path, record_edition=True)
    results[0]['fields']['value']['text'] = '5'
    engine.write(tmp_path / 'investigation.json', dict(raw, claims=[claim], study_results=results))
    issues = engine.bind_payload(tmp_path, record_edition=True)['mechanical_audit']['issues']
    assert [i for i in issues if i.startswith('Study table: claim uses changed value')] == [
        f"Study table: claim uses changed value (claim id {claim['id']}, study result id SR1, edition 2, "
        f"fields ['value'], summary indexes {[i for i, e in enumerate(raw['executive_summary'], 1) if isinstance(e, dict) and claim['id'] in e.get('claim_ids', [])]})"]
