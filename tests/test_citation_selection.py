import pytest

from canary import cli, coverage_export, coverage_runtime, research_spec, research_tools, runs
from canary.candidate_index import CandidateIndex
from test_api_contract import api, validate_response
from test_coverage_evidence_ui import run_dashboard


def work(identifier, references=(), **metadata):
    return {'id': 'https://openalex.org/' + identifier, 'title': identifier,
            'referenced_works': ['https://openalex.org/' + work_id for work_id in references], **metadata}


def test_citation_opt_in_preserves_the_question_and_source_boundary():
    question = {'question': 'Which protocol measures a switching interval?', 'study_brief': 'Compare two clocks.'}
    default = research_spec.compile_mapping(question)['resolved']
    assert default['retrieval']['selection'] == 'coverage'
    spec = dict(question, retrieval={'selection': 'citation_graph'})
    packet = research_spec.compile_mapping(spec)
    assert research_spec.verify_compiled(packet)['resolved']['retrieval']['selection'] == 'citation_graph'
    with pytest.raises(research_spec.CompileError, match='requires OpenAlex'):
        research_spec.compile_mapping(dict(spec, retrieval={'selection': 'citation_graph', 'corpus': 'local_text'}))


def test_cli_passes_the_flag_and_brief_over_the_api(api, monkeypatch):
    server, client, _ = api
    monkeypatch.setenv('CANARY_RUNS_PORT', str(server.server_port))
    assert cli.main(['ask', 'Which protocol?', '--citation-graph', '--study-brief', 'Compare two clocks.']) == 0
    resolved = client.get('/api/runs/synthetic/bundle').raise_for_status().json()['bundle']['specification']['resolved']
    assert resolved['question'] == 'Which protocol?'
    assert resolved['study_brief'] == 'Compare two clocks.'
    assert resolved['retrieval']['selection'] == 'citation_graph'


def test_api_returns_only_new_within_scope_edges_and_preserves_decisions(api, monkeypatch):
    server, client, root = api
    workspace = root/'citation-pilot'
    coverage_runtime.write(workspace/'run.json', {'status': 'researching', 'kind': 'question',
                                                 'citation_graph_requested': True})
    runs.register_start('citation-pilot', 'Citation pilot', '', kind='question', bundle=str(workspace),
                        path=server.RequestHandlerClass.supervisor.registry)
    decisions = [{'work_id': 'W1', 'decision': 'include'}, {'work_id': 'W2', 'decision': 'include'},
                 {'work_id': 'W3', 'decision': 'exclude'}]
    coverage_runtime.write(workspace/'selection.json', {'decisions': decisions})
    with CandidateIndex(workspace) as index:
        index.add_page([work('W1'), work('W2'), work('W3', ['W1','W2']),
                        work('W4', ['W1','W2'], doi='10.1234/example'), work('W5', ['W1']),
                        work('W6', ['W1','W2'], is_retracted=True),
                        work('W7', ['W1','W2'], doi='https://doi.org/10.1234/example')], 'scope')
        index.add_page([work('W8', ['W1','W2'])], 'unrelated')
    route = '/api/runs/citation-pilot/tools/citation-leads'
    invalid = client.post(route, json={'anchors': ['W3']})
    assert invalid.status_code == 400
    acquire = '/api/runs/citation-pilot/tools/acquire'
    pending = client.post(acquire, json={'ids': ['W1', 'W2']})
    assert pending.status_code == 400
    assert 'W4, W5' in pending.json()['error']
    response = client.post(route, json={'anchors': ['W1','W2']})
    assert response.status_code == 200, response.text
    report = response.json()
    validate_response(client.get('/openapi.json').json(), '/api/runs/{key}/tools/citation-leads', 'post', 200, report)
    assert [(row['work_id'], row['cites_anchors']) for row in report['leads']] == [('W4',['W1','W2']), ('W5',['W1'])]
    coverage = coverage_export.export_bundle(workspace)['coverage']
    assert coverage['citation_leads'] == report
    assert coverage['unscreened_citation_leads'] == ['W4','W5']
    coverage_runtime.write(workspace/'selection.json', {'decisions': decisions + [{'work_id':'W4','decision':'defer'}]})
    assert coverage_export.export_bundle(workspace)['coverage']['unscreened_citation_leads'] == ['W5']
    pending = client.post(acquire, json={'ids': ['W1']})
    assert 'Screen the recorded citation leads before acquisition: W5' == pending.json()['error']

    decisions.extend([{'work_id': 'W4', 'decision': 'defer'}, {'work_id': 'W5', 'decision': 'include'}])
    coverage_runtime.write(workspace/'selection.json', {'decisions': decisions})
    acquired = []
    def acquire_fixture(path, ids, **options):
        acquired.extend(ids)
        return []
    monkeypatch.setattr(coverage_runtime, 'acquire', acquire_fixture)
    row = {'bundle': str(workspace), 'status': 'researching'}
    with pytest.raises(ValueError, match='requires an include decision'):
        research_tools.execute(row, 'acquire', {'ids': ['W1', 'W4']}, root)
    research_tools.execute(row, 'acquire', {'ids': ['W1', 'W5']}, root)
    assert acquired == ['W1', 'W5']

    coverage_runtime.write(workspace/'run.json', {'status': 'researching', 'kind': 'question'})
    research_tools.execute(row, 'acquire', {'ids': ['W1', 'W4']}, root)
    assert acquired == ['W1', 'W5', 'W1', 'W4']


def test_dashboard_passes_both_inputs_and_shows_citation_provenance(tmp_path):
    run_dashboard(tmp_path, {}, r'''
const launch=buildLaunch({mode:"question",q:"Which protocol?",selection:"citation_graph","study-brief":"Compare A and B."});
assert.deepEqual(launch.spec,{question:"Which protocol?",study_brief:"Compare A and B.",retrieval:{selection:"citation_graph"}});
assert.throws(()=>buildLaunch({q:"Which protocol?",corpus:"local_text",selection:"citation_graph"}),/public literature/);
const coverage={citation_leads:{anchors:["W1"],openalex_works:5,works_with_references:3,
  leads:[{work_id:"W2",title:"<source>",cites_anchors:["W1"],shared_query_ids:["design"],incoming_links:0}]},
  unscreened_citation_leads:["W2"]};
const html=citationLeadsHtml({run:{citation_graph_requested:true},coverage});
assert.match(html,/Awaiting screening/);
assert.match(html,/openalex.org\/W1/);
assert.match(html,/&lt;source&gt;/);
''')
