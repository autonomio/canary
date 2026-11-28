import json
from types import SimpleNamespace

import jsonschema

from canary import coverage_protocol, coverage_runtime as engine, research_bundle, research_spec, study_editions, synthesis
from test_corrections import researched
from test_study_table import VERSIONS, result


def synthesis_record(**changes):
    record = {'id': 'Y1', 'outcome': 'Breaking force', 'comparison': 'Higher against lower infill',
              'study_result_ids': ['SR1', 'SR2'],
              'grouping': {'decision': 'combined', 'reason': 'Same test and follow-up.', 'kept_apart_from': []},
              'time_points': '24 hours', 'effect_measure': 'Mean difference', 'missing_values': 'None missing.',
              'method': {'kind': 'narrative', 'description': 'Direction of effect by study.'},
              'explanation': {'direction': 'Higher infill raised breaking force in both studies.',
                              'size': 'Around 1 kN in each.', 'differences': 'Both tested the same polymer.',
                              'design_and_bias': 'Both were unblinded bench tests.'},
              'swim': None, 'display': 'Table of study results.', 'claim_ids': ['C1']}
    record.update(changes)
    return record


RESULTS = [result(), result(id='SR2', study_id='S2')]
CLAIMS = [{'id': 'C1'}]


def kinds(syntheses, results=RESULTS, versions=VERSIONS):
    return sorted((i['kind'], i.get('study_result_id') or i.get('study_id') or i.get('claim_id') or i.get('kept_apart_from')
                   or i.get('outcome_name')) for i in synthesis.issues(syntheses, results, CLAIMS, versions))


def test_a_synthesis_matching_the_study_table_has_no_issues():
    shape = coverage_protocol.investigation_schema()['properties']['syntheses']['items']
    jsonschema.Draft202012Validator(shape).validate(synthesis_record())
    assert kinds([synthesis_record()]) == []


def test_each_synthesis_is_checked_against_results_outcomes_studies_and_claims():
    named = [dict(result(id='SR3', study_id='S1'), outcome={'id': 'O2', 'protocol_version': 'V1', 'name': 'Elongation'})]
    record = synthesis_record(study_result_ids=['SR1', 'SR3', 'SR9'], claim_ids=['C9'],
                              grouping={'decision': 'combined', 'reason': 'Same test.', 'kept_apart_from': ['Y7', 'Y1']})
    assert kinds([record], RESULTS + named) == [
        ('repeated_synthesis_study', 'S1'), ('synthesis_outcome_mismatch', 'SR3'), ('unknown_kept_apart_synthesis', 'Y1'),
        ('unknown_kept_apart_synthesis', 'Y7'), ('unknown_synthesis_claim', 'C9'), ('unknown_synthesis_result', 'SR9')]
    assert kinds([synthesis_record(outcome='Breaking forse')]) == [
        ('synthesis_outcome_mismatch', 'SR1'), ('synthesis_outcome_mismatch', 'SR2'), ('unknown_synthesis_outcome', 'Breaking forse')]
    assert kinds([synthesis_record(study_result_ids=[]), synthesis_record()]) == [
        ('duplicate_synthesis_id', None), ('synthesis_without_results', None)]
    apart = synthesis_record(id='Y2', grouping={'decision': 'kept_separate', 'reason': 'Six months answers another question.',
                                                'kept_apart_from': ['Y1']}, study_result_ids=['SR1', 'SR1'])
    assert kinds([synthesis_record(), apart]) == []


def test_a_synthesis_in_words_explains_its_findings_and_how_it_was_summarised():
    shape = coverage_protocol.investigation_schema()['properties']['syntheses']['items']
    swim = {'technique': 'vote_counting_direction', 'prioritisation': 'Tensile test results before flexural ones.',
            'limitations': 'Counts ignore the size of each effect.'}
    jsonschema.Draft202012Validator(shape).validate(synthesis_record(method={'kind': 'swim', 'description': 'Vote count.'}, swim=swim))
    assert kinds([synthesis_record(method={'kind': 'swim', 'description': 'Vote count.'}, swim=swim)]) == []
    assert kinds([synthesis_record(explanation=None)]) == [('synthesis_without_explanation', None)]
    assert kinds([synthesis_record(method={'kind': 'statistical', 'description': 'Fixed effect.'}, explanation=None)]) == []
    blank = {'direction': ' ', 'size': None, 'differences': '', 'design_and_bias': 'Bench tests.'}
    assert [(i['kind'], i['field']) for i in synthesis.issues([synthesis_record(explanation=blank)], RESULTS, CLAIMS, VERSIONS)] == [
        ('explanation_missing', 'direction'), ('explanation_missing', 'differences'), ('explanation_missing', 'size')]
    unestimated = [dict(r, outcome_result=dict(r['outcome_result'], estimate=None)) for r in RESULTS]
    assert kinds([synthesis_record(explanation=dict(blank, direction='Up', differences='None'))], unestimated) == []
    assert kinds([synthesis_record(method={'kind': 'swim', 'description': 'Vote count.'}, swim=swim)], unestimated) == []
    summary = dict(swim, technique='summary_of_effects')
    assert kinds([synthesis_record(method={'kind': 'swim', 'description': 'Median effect.'}, swim=summary)], unestimated) == [
        ('swim_without_effect_estimates', None)]
    assert kinds([synthesis_record(method={'kind': 'swim', 'description': 'Median effect.'}, swim=summary)]) == []
    assert kinds([synthesis_record(method={'kind': 'swim', 'description': 'Vote count.'})]) == [('swim_without_details', None)]
    unfinished = synthesis_record(method={'kind': 'swim', 'description': 'Vote count.'}, swim=dict(swim, prioritisation='', limitations=' '))
    assert [(i['kind'], i['field']) for i in synthesis.issues([unfinished], RESULTS, CLAIMS, VERSIONS)] == [
        ('swim_missing', 'prioritisation'), ('swim_missing', 'limitations')]
    apart = synthesis_record(grouping={'decision': 'kept_separate', 'reason': '', 'kept_apart_from': []})
    assert kinds([apart]) == [('grouping_without_reason', None)]


def test_the_export_names_each_contributing_study_once():
    exported, = synthesis.export([synthesis_record(study_result_ids=['SR1', 'SR3', 'SR2', 'SR1'])],
                                 RESULTS + [result(id='SR3', study_id='S1')])
    assert exported['study_ids'] == ['S1', 'S2']


def investigation(value='4.2', comparison='Higher against lower infill'):
    raw = {'claims': [{'id': 'C1', 'text': 'Force rose.', 'study_result_ids': ['SR1']}], 'studies': [],
           'study_results': [dict(result(), outcome_result=dict(result()['outcome_result'],
                                                                estimate={'text': value, 'passage_ids': [], 'origin': 'reported'}))],
           'syntheses': [synthesis_record(study_result_ids=['SR1'], comparison=comparison)]}
    return raw


def test_a_synthesis_left_unchanged_after_its_input_changed_is_stale(tmp_path):
    references = {'R1': {'text_sha256': 't1', 'body_sha256': 'b1'}}
    study_editions.record(tmp_path, investigation(), references)
    study_editions.record(tmp_path, investigation(value='3.9'), references)
    editions = study_editions.history(tmp_path)
    assert editions[0]['syntheses']['Y1']['comparison'] == 'Higher against lower infill'
    assert synthesis.stale(editions, investigation(value='3.9')) == [
        {'kind': 'synthesis_uses_changed_value', 'synthesis_id': 'Y1', 'study_result_id': 'SR1', 'edition': 2,
         'fields': ['estimate']}]
    study_editions.record(tmp_path, investigation(value='3.9', comparison='Revised comparison'), references)
    editions = study_editions.history(tmp_path)
    change, = editions[2]['changes']
    assert change['synthesis_id'] == 'Y1' and change['fields'] == ['comparison']
    assert synthesis.stale(editions, investigation(value='3.9', comparison='Revised comparison')) == []


def test_syntheses_sharing_an_id_are_kept_in_editions_without_breaking_later_changes(tmp_path):
    references = {'R1': {'text_sha256': 't1', 'body_sha256': 'b1'}}
    def twice(value, comparison):
        raw = investigation(value=value)
        raw['syntheses'].append(synthesis_record(study_result_ids=['SR1'], comparison=comparison))
        return raw
    study_editions.record(tmp_path, twice('4.2', 'First'), references)
    study_editions.record(tmp_path, twice('3.9', 'First'), references)
    assert synthesis.stale(study_editions.history(tmp_path), twice('3.9', 'First')) == []
    study_editions.record(tmp_path, twice('3.9', 'Second'), references)
    change, = study_editions.history(tmp_path)[2]['changes']
    assert change['synthesis_id'] == 'Y1' and change['fields'] == ['entries']
    assert [e['comparison'] for e in change['after']['entries']] == ['Higher against lower infill', 'Second']


def test_runs_without_syntheses_keep_their_edition_contents():
    raw = investigation()
    raw.pop('syntheses')
    assert 'syntheses' not in study_editions.contents(raw, {})


def test_synthesis_defects_are_in_the_mechanical_audit_and_the_export(tmp_path):
    from canary import coverage_export

    research_bundle.initialize(tmp_path, research_spec.compile_mapping(
        {'question': 'Synthetic fixture question?', 'retrieval': {'target_papers': 1}}))
    researched(tmp_path)
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), study_table_version=1))
    raw = json.loads((tmp_path / 'investigation.json').read_text())
    raw['syntheses'] = [synthesis_record(outcome=None, study_result_ids=['SR9'], claim_ids=[])]
    engine.write(tmp_path / 'investigation.json', raw)
    assert not [i for i in engine.bind_payload(tmp_path)['mechanical_audit']['issues'] if i.startswith('Synthesis')]
    engine.write(tmp_path / 'run.json', dict(engine.read(tmp_path / 'run.json'), synthesis_version=1))
    issues = [i for i in engine.bind_payload(tmp_path)['mechanical_audit']['issues'] if i.startswith('Synthesis')]
    assert issues == ['Synthesis: unknown synthesis result (synthesis id Y1, study result id SR9)']
    graph = coverage_export.export_bundle(tmp_path)['answer']
    assert graph['syntheses'][0]['id'] == 'Y1' and graph['syntheses'][0]['study_ids'] == []
    assert [i['kind'] for i in graph['binding_issues'] if i.get('section') == 'syntheses'] == ['unknown_synthesis_result']


def test_the_syntheses_api_serves_the_recorded_findings(tmp_path, monkeypatch):
    from canary import dashboard, runs

    answer = {'syntheses': [{'id': 'Y1'}], 'binding_issues': [{'kind': 'synthesis_without_results', 'section': 'syntheses'},
                                                              {'kind': 'lacks_evidence'}]}
    handler = object.__new__(dashboard.Handler)
    handler.supervisor = SimpleNamespace(root=tmp_path, registry=None)
    handler.dashboard_enabled = True
    responses = []
    handler._send = lambda *args: responses.append(args)
    monkeypatch.setattr(runs, 'get', lambda key, registry: {'key': key, 'bundle': str(tmp_path)} if key == 'review' else None)
    monkeypatch.setattr(dashboard, 'research_for_row', lambda row, supervisor: ({'answer': answer}, False))
    for path in ('/api/runs/review/syntheses', '/api/runs/missing/syntheses'):
        handler.path = path
        handler.do_GET()
    answer.pop('syntheses')
    handler.path = '/api/runs/review/syntheses'
    handler.do_GET()
    assert [status for status, _ in responses] == [200, 404, 409]
    assert responses[0][1] == {'key': 'review', 'syntheses': [{'id': 'Y1'}],
                               'issues': [{'kind': 'synthesis_without_results', 'section': 'syntheses'}]}


def test_the_dashboard_shows_each_synthesis_with_its_grouping_and_flags(tmp_path):
    from test_coverage_evidence_ui import run_dashboard

    apart = synthesis_record(id='Y2', grouping={'decision': 'kept_separate', 'reason': 'Six months answers another question.',
                                                'kept_apart_from': ['Y1']}, method={'kind': 'swim', 'description': 'Vote count.'},
                             swim={'technique': 'vote_counting_direction', 'prioritisation': 'Tensile first.', 'limitations': 'Ignores size.'})
    graph = {'syntheses': synthesis.export([synthesis_record(), apart], RESULTS), 'claims': [{'id': 'C1'}],
             'binding_issues': [{'kind': 'repeated_synthesis_study', 'section': 'syntheses', 'synthesis_id': 'Y1', 'study_id': 'S1'},
                                {'kind': 'duplicate_synthesis_id', 'section': 'syntheses', 'synthesis_id': 'Y2'}]}
    run_dashboard(tmp_path, graph, r'''
const html=synthesesHtml(graph);
assert.match(html,/Syntheses · 2<\/summary>/);
assert.match(html,/<summary>Y1 · Breaking force — Higher against lower infill <span class="hint">combined · 2 studies · narrative · 1 flag<\/span>/);
assert.match(html,/<p role="alert">Synthesis Y1 combines more than one result from study S1\.<\/p>/);
assert.match(html,/<dt>Grouping<\/dt><dd><p>Kept separate<\/p><p>Six months answers another question\.<\/p><p class="hint">Kept apart from Y1<\/p>/);
assert.match(html,/Synthesis without meta-analysis \(SWiM\)<\/p><p>Vote count\.<\/p>/);
assert.match(html,/<p class="hint">Vote counting on direction of effect<\/p><p class="hint">Prioritised by<\/p><p>Tensile first\.<\/p><p class="hint">Limitations<\/p><p>Ignores size\.<\/p>/);
assert.match(html,/<dt>Direction<\/dt><dd><p>Higher infill raised breaking force in both studies\.<\/p><\/dd><dt>Size<\/dt><dd><p>Around 1 kN in each\.<\/p>/);
assert.match(html,/<dt>Design and bias<\/dt><dd><p>Both were unblinded bench tests\.<\/p><\/dd>/);
assert.equal(synthesisIssueText({kind:'swim_missing',synthesis_id:'Y1',field:'limitations'}),'Synthesis Y1 uses SWiM but does not state the limitations of its technique.');
assert.equal(synthesisIssueText({kind:'explanation_missing',synthesis_id:'Y1',field:'size'}),'Synthesis Y1 does not explain the size of the findings, which its results report.');
assert.match(html,/data-qa-study="SR2"/);
assert.match(html,/data-qa-claim="C1">C1</);
assert.match(html,/Synthesis issues \(1\)<\/p><ul><li>Synthesis Y2 is recorded more than once\./);
assert.equal(synthesesHtml({}),'');
const edition=(n,comparison)=>({number:n,sha256:'s'+n,recorded_at:'2026-11-28T10:00:00Z',results:{},studies:{},syntheses:{Y1:{comparison}},
  changes:n===1 ? [] : [{synthesis_id:'Y1',fields:['comparison'],before:{comparison:'Old'},after:{comparison}}]});
const editions=studyEditionsHtml({study_table_editions:[edition(1,'Old'),edition(2,'New')]});
assert.match(editions,/Synthesis Y1 · comparison/);
assert.match(editions,/&quot;syntheses&quot;: \{\s+&quot;Y1&quot;: \{\s+&quot;comparison&quot;: &quot;New&quot;/);
assert.doesNotMatch(studyEditionsHtml({study_table_editions:[{...edition(1,'Old'),syntheses:undefined},{...edition(2,'Old'),syntheses:undefined}]}),/syntheses/);
assert.ok(qaGraph({answer:{syntheses:graph.syntheses}}));
''')
