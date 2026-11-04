"""Continuation input contracts; fixtures are synthetic and contain no TempRest data."""
import json

import jsonschema
import pytest

from canary import api_contract, coverage_protocol, research_bundle, research_spec, research_state
from test_api_contract import api
from test_research_bundle import seed
from test_coverage_evidence_ui import run_dashboard


STATE = {'claims': [{'id': 'C-prior', 'statement': 'Synthetic prior finding.'}],
         'references': [{'id': 'W-old', 'doi': '10.0000/synthetic'}],
         'reviewed_papers': [{'work_id': 'W-old', 'eligibility': 'core'}],
         'provenance': {'repository': 'example/world', 'revision': 'a' * 40}}


def test_context_roundtrip_and_changed_state_invalidate_recorded_inputs(tmp_path):
    packet = research_spec.compile_mapping({'question': 'Synthetic question?', 'research_state': STATE,
                                            'continue_research': True})
    assert research_spec.verify_compiled(packet) == packet
    assert packet['resolved']['retrieval']['target_papers'] == 50
    assert packet['resolved']['audit']['budget_seconds'] is None
    assert packet['resolved']['research']['model'] == coverage_protocol.RESEARCH_MODEL
    research_bundle.initialize(tmp_path, packet)
    seed(tmp_path)
    research_bundle.seal(tmp_path)
    assert research_bundle.read(tmp_path)['specification']['resolved']['research_state'] == STATE
    (tmp_path / 'research-state.json').write_text('{}')
    with pytest.raises(ValueError, match='research-state'):
        research_bundle.read(tmp_path)


@pytest.mark.parametrize('options', [
    {'continue_research': True}, {'continue_research': 'true', 'research_state': STATE},
    {'research_state': []}, {'research_state': {}}, {'research_state': {'bad': float('nan')}},
    {'research_state': {'large': 'x' * research_state.MAX_STATE_BYTES}},
])
def test_invalid_context_is_rejected_before_a_run(options):
    with pytest.raises(research_spec.CompileError):
        research_spec.compile_mapping({'question': 'Synthetic question?', **options})


def test_http_large_context_compile_and_legacy_question_share_inputs(api, monkeypatch):
    server, client, _ = api
    state = dict(STATE, note='x' * (5 * 1024 * 1024))
    body = {'question': 'Synthetic continuation?', 'research_state': state, 'continue_research': True}
    response = client.post('/api/runs/compile', json={'spec': body})
    assert response.status_code == 200, response.text
    assert response.json()['resolved']['research_state'] == state
    jsonschema.validate(body, api_contract.schemas()['ResearchDeclaration'])
    captured = {}
    def start(question, name='', brief='', **context):
        captured.update(question=question, **context)
        return {'key': 'synthetic'}
    monkeypatch.setattr(server.RequestHandlerClass.supervisor, 'start_question', start)
    response = client.post('/api/runs', json=dict(body, kind='question'))
    assert response.status_code == 201, response.text
    assert captured == body
    schema = client.get('/openapi.json').json()['components']['schemas']['ResearchDeclaration']
    assert 'research_state' in schema['properties'] and 'continue_research' in schema['properties']


def test_context_is_only_an_explicit_exception_and_fresh_work_is_not_relabelled():
    normal = coverage_protocol.research_prompt('Q?', 'tools')
    continued = coverage_protocol.research_prompt('Q?', 'tools', research_state=True, continue_research=True)
    assert 'No earlier experiment assets' in normal
    assert 'No earlier experiment assets' not in continued
    for phrase in ('research-state.json', 'previously reviewed publications', 'unchanged paper target',
                   'untrusted source data', "this run's inspected sources"):
        assert phrase in continued
    assert 'at least 50' in normal and 'at least 50' in continued


def test_dashboard_accepts_context_and_exposes_the_exact_starting_state(tmp_path):
    run_dashboard(tmp_path, STATE, r'''
const before=buildLaunch({q:'Q?',mode:'question'});
assert.deepEqual(before,{kind:'question',question:'Q?',name:'Q?',brief:''});
const payload=buildLaunch({q:'Q?',mode:'question',research_state:JSON.stringify(graph),continue_research:true});
assert.deepEqual(payload.research_state,graph);
assert.equal(payload.continue_research,true);
assert.throws(()=>buildLaunch({q:'Q?',continue_research:true}),/Supply research state/);
assert.throws(()=>buildLaunch({q:'Q?',research_state:'broken'}),/valid JSON/);
assert.throws(()=>buildLaunch({q:'Q?',mode:'cycle',research_state:graph}),/question research flow/);
const html=researchStateHtml({research_state:{claims:[{text:'<unsafe>'}],references:[],reviewed_papers:[]},continue_research:true});
assert.match(html,/Continuing research/);assert.match(html,/&lt;unsafe&gt;/);assert.doesNotMatch(html,/<unsafe>/);
''')


def test_supplied_state_survives_working_file_release(tmp_path):
    from canary import compact_bundle
    packet=research_spec.compile_mapping({'question':'Synthetic continuation?',
        'research_state':STATE,'continue_research':True})
    (tmp_path/'.run-reservation').touch()
    research_bundle.initialize(tmp_path,packet);seed(tmp_path);research_bundle.seal(tmp_path)
    compact_bundle.retain(tmp_path)
    resolved=research_bundle.read(tmp_path)['specification']['resolved']
    assert resolved['research_state']==STATE and resolved['continue_research'] is True
    assert not (tmp_path/'research-state.json').exists()


def test_unicode_state_within_byte_limit_survives_generated_yaml():
    state={'note':'😀' * 240_000}
    packet=research_spec.compile_mapping({'question':'Synthetic Unicode context?', 'research_state':state,
                                         'continue_research':True})
    assert research_spec.verify_compiled(packet)['resolved']['research_state']==state


def test_yaml_alias_graph_is_rejected_before_serialization():
    lines=['question: Synthetic alias bound?', 'research_state:', '  a: &a [x,x,x,x,x,x,x,x,x,x]']
    for parent, child in zip('abcdefgh', 'bcdefghi'):
        lines.append('  '+child+': &'+child+' ['+','.join(['*'+parent]*10)+']')
    with pytest.raises(research_spec.CompileError, match='aliases'):
        research_spec.compile_research('\n'.join(lines))


def test_cyclic_or_excessively_deep_state_is_rejected():
    state={};state['cycle']=state
    with pytest.raises(research_spec.CompileError, match='cycles'):
        research_spec.compile_mapping({'question':'Synthetic cycle?', 'research_state':state})
    state={};head=state
    for _ in range(80):
        head['child']={};head=head['child']
    with pytest.raises(research_spec.CompileError, match='nesting'):
        research_spec.compile_mapping({'question':'Synthetic depth?', 'research_state':state})


def test_ten_mib_boundary_is_inclusive_in_encoded_json_bytes():
    empty_bytes = len(json.dumps({'note': ''}, ensure_ascii=False, sort_keys=True).encode())
    state = {'note': 'x' * (research_state.MAX_STATE_BYTES - empty_bytes)}
    assert research_state.validate(state, True) == state
    with pytest.raises(ValueError, match='10 MiB'):
        research_state.validate({'note': state['note'] + 'x'}, True)


def test_declaration_error_reports_the_enforced_limit(monkeypatch):
    message = 'Expected a nonempty YAML declaration of at most {} MiB.'
    with pytest.raises(research_spec.CompileError) as caught:
        research_spec.compile_research('x' * (research_state.MAX_DECLARATION_BYTES + 1))
    assert caught.value.issues == [{'path': 'yaml', 'message': message.format('20')}]

    limit = 512 * 1024
    monkeypatch.setattr(research_spec, 'MAX_DECLARATION_BYTES', limit)
    source = 'question: Synthetic?\n#'
    source += 'x' * (limit - len(source))
    assert research_spec.compile_research(source)['resolved']['question'] == 'Synthetic?'
    with pytest.raises(research_spec.CompileError) as caught:
        research_spec.compile_research(source + 'x')
    assert caught.value.issues == [{'path': 'yaml', 'message': message.format('0.5')}]
