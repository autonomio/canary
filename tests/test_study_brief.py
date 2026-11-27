import pytest

from canary import compact_bundle, coverage_protocol, coverage_runtime, research_bundle, research_spec
from test_api_contract import api
from test_coverage_evidence_ui import run_dashboard
from test_research_bundle import seed


QUESTION = 'Which measurements distinguish first crossing from sustained delivery?'
BRIEF = 'Use a paired bench design.\nCompare timing on a common clock.'


def test_http_requires_a_question_and_saves_optional_brief(api):
    _, client, _ = api
    invalid = client.post('/api/runs/compile', json={'spec': {'study_brief': BRIEF}})
    assert invalid.status_code == 400
    assert invalid.json()['issues'][0]['path'] == 'question'
    response = client.post('/api/runs', json={'spec': {'question': QUESTION, 'study_brief': BRIEF}})
    assert response.status_code == 201, response.text
    bundle = client.get('/api/runs/synthetic/bundle').raise_for_status().json()['bundle']
    assert bundle['specification']['resolved']['study_brief'] == BRIEF


@pytest.mark.parametrize('brief', [None, '', '   ', 'a' * 20_001])
def test_submitted_brief_must_be_bounded_text(brief):
    with pytest.raises(research_spec.CompileError, match='study_brief'):
        research_spec.compile_mapping({'question': QUESTION, 'study_brief': brief})


def test_brief_reaches_research_as_context_and_survives_the_archive(tmp_path, monkeypatch):
    spec = {'question': QUESTION, 'study_brief': '\n' + BRIEF + '\n',
            'retrieval': {'selection': 'citation_graph'}}
    packet = research_spec.compile_mapping(spec)
    assert research_spec.verify_compiled(packet) == packet
    workspace = tmp_path/'research'
    clarification = {'question': 'Which clock?', 'reason': 'Fixture'}
    def scientific_session(path, role, model, effort, prompt, *_args, **_kwargs):
        assert role == 'research'
        assert coverage_protocol.STUDY_BRIEF_SCOPE in prompt
        assert 'citation-leads --anchors' in prompt
        assert f'Question: {QUESTION}' in prompt
        assert BRIEF not in prompt
        assert coverage_runtime.read(path/'run-spec.json')['study_brief'] == spec['study_brief']
        coverage_runtime.write(path/'investigation.json', {'clarification': clarification})
    monkeypatch.setattr(coverage_runtime, 'scientific_session', scientific_session)
    result = coverage_runtime.run(QUESTION, workspace, executable='fixture',
                                  on_reserved=lambda: research_bundle.initialize(workspace, packet))
    assert result['clarification'] == clarification
    manifest = coverage_runtime.read(workspace/'run.json')
    assert manifest['study_brief_supplied'] and manifest['citation_graph_requested']
    seed(workspace)
    research_bundle.seal(workspace)
    compact_bundle.retain(workspace)
    assert research_bundle.read(workspace)['specification']['resolved']['study_brief'] == spec['study_brief']


def test_dashboard_uses_one_question_flow_and_displays_the_recorded_brief(tmp_path):
    run_dashboard(tmp_path, {}, r'''
const launch=buildLaunch({q:"Which outcome?",mode:"question","study-brief":"Compare A and B."});
assert.deepEqual(launch.spec,{question:"Which outcome?",study_brief:"Compare A and B."});
assert.throws(()=>buildLaunch({q:"","study-brief":"Compare A and B."}),/research question/);
const local=buildLaunch({q:"Which outcome?",mode:"question",corpus:"local_text",directory:"/papers","study-brief":"Compare A and B."});
assert.equal(buildLaunch({q:"Which outcome?","protocol-author":"A. Rivera"}).spec.protocol_author,"A. Rivera");
assert.throws(()=>buildLaunch({q:"Which outcome?",argv:"custom","protocol-author":"A. Rivera"}),/Protocol authors/);
assert.equal(local.spec.study_brief,"Compare A and B.");
assert.equal(local.spec.retrieval.corpus,"local_text");
assert.match(studyBriefHtml({study_brief:"<brief>"}),/&lt;brief&gt;/);
''')
