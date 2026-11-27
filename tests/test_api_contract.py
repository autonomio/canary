"""Verify the live contract and a real stdio MCP relay, with synthetic science."""
import asyncio
import io
import json
from pathlib import Path
import sys
import subprocess
import threading
import zipfile

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from canary import (api_client, api_contract, bundle_encoding, cli, compact_bundle, coverage_export,
                    coverage_protocol, coverage_runtime, dashboard, keyword_search, research_bundle, runs)
from canary.mcp_server import tool_catalog
from test_coverage_evidence_ui import run_dashboard
from test_research_bundle import seed


@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.delenv('CANARY_API_URL',raising=False)
    server = dashboard.make_server(0,root=tmp_path,registry=tmp_path/'registry.json')
    supervisor = server.RequestHandlerClass.supervisor
    def start(packet,name='',brief=''):
        workspace = tmp_path/'synthetic';workspace.mkdir()
        research_bundle.initialize(workspace,packet);seed(workspace)
        coverage_export.export_bundle(workspace);research_bundle.seal(workspace)
        runs.register_start('synthetic','Synthetic interface fixture','No scientific evidence',
                            kind='question',bundle=str(workspace),status='done',path=supervisor.registry)
        return {'key':'synthetic','bundle':str(workspace),'spec_sha256':packet['spec_sha256']}
    monkeypatch.setattr(supervisor,'start_compiled',start)
    monkeypatch.setattr(supervisor.auth,'status',lambda:{'available':True,'authenticated':False,
        'state':'signed_out','mode':None,'email':None,'login':None,'error':None})
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=20,trust_env=False) as client:
        yield server,client,tmp_path
    server.shutdown();server.server_close();thread.join(5)


def validate_response(contract,path,method,status,value):
    schema = contract['paths'][path][method]['responses'][str(status)]['content']['application/json']['schema']
    Draft202012Validator(dict(schema,components=contract['components'])).validate(value)


def test_keyword_search_and_cli_use_one_page_and_honor_recorded_holds(api, monkeypatch):
    server, client, _ = api
    monkeypatch.delenv('OPENALEX_API_KEY', raising=False)
    requests = []

    def provider(request):
        assert request.url.host == 'api.openalex.org'
        requests.append(request.url.params['search'])
        if len(requests) == 2:
            return httpx.Response(429, headers={'Retry-After': '120'})
        return httpx.Response(200, json={'meta': {'count': 2}, 'results': [
            {'id': 'https://openalex.org/W1', 'title': 'First result', 'publication_year': 2025},
            {'id': 'https://openalex.org/W2', 'title': 'Second result'},
        ]})

    search = keyword_search.search
    with httpx.Client(transport=httpx.MockTransport(provider)) as provider_client:
        monkeypatch.setattr(keyword_search, 'search', lambda root, body: search(root, body, client=provider_client))
        path = '/api/sources/openalex/search'
        contract = client.get('/openapi.json').json()
        assert contract['paths'][path]['post']['x-canary-mcp']['readOnly'] is False
        assert client.post(path, json={'query': '', 'limit': 15}).status_code == 400
        assert client.post(path, json={'query': 'Lake', 'limit': 201}).status_code == 400
        first = client.post(path, json={'query': 'Lake water', 'limit': 1})
        assert first.status_code == 200, first.text
        validate_response(contract, path, 'post', 200, first.json())
        assert first.json()['matched'] == 2
        assert [p['title'] for p in first.json()['results']] == ['First result']
        paused = client.post(path, json={'query': 'River water'}).json()
        validate_response(contract, path, 'post', 200, paused)
        assert paused['status'] == 'paused' and paused['provider_status'] == 429
        command = subprocess.run([sys.executable, '-m', 'canary.cli', 'sources', 'search',
                                  'Forest soil', '--port', str(server.server_port)],
                                 capture_output=True, text=True, timeout=15)
        assert command.returncode == 0, command.stderr
        assert json.loads(command.stdout)['status'] == 'paused'
        assert requests == ['Lake water', 'River water']
        assert client.get('/api/runs').json() == {'runs': []}


def test_live_openapi_cli_and_responses_share_the_contract(api,capsys):
    server,client,_ = api
    contract = client.get('/openapi.json').json()
    assert contract['openapi'] == '3.1.0'
    ids=[]
    for path,methods in contract['paths'].items():
        for method,operation in methods.items(): ids.append(operation['operationId'])
    assert len(ids)==len(set(ids))
    for schema in contract['components']['schemas'].values(): Draft202012Validator.check_schema(schema)
    export = contract['paths']['/api/runs/{key}/exports/{connector}']['get']
    representation = next(p for p in export['parameters'] if p['name']=='representation')
    assert representation['schema'] == {'enum':['compact','expanded'],'default':'compact'}
    assert contract['components']['schemas']['ReferenceEncoding']['properties']['version']['enum'] == [1]
    assert 'compressed' not in contract['components']['schemas']['ReferenceEncoding']['required']
    assert export['responses']['422']['content']['application/json']['schema'] == {'$ref': '#/components/schemas/ContributionTooLarge'}
    assert cli.main(['api','--port',str(server.server_port)]) == 0
    assert json.loads(capsys.readouterr().out) == contract
    for path in ('/api/status','/api/connection','/api/auth/status','/api/auth/usage','/api/runs','/api/log','/api/usage',
                 '/api/sources/openalex/status'):
        response=client.get(path);assert response.status_code==200,response.text
        validate_response(contract,path,'get',200,response.json())
    discovery=client.get('/api/sources/openalex/status').json()['discovery']
    assert discovery['status']=='available' and discovery['backoff'] is None
    assert discovery['probe_request_made'] is False and discovery['basis']=='recorded_backoff_only'
    recheck=client.post('/api/sources/openalex/recheck',json={})
    assert recheck.status_code==200,recheck.text
    validate_response(contract,'/api/sources/openalex/recheck','post',200,recheck.json())
    assert recheck.json()['status']=='no_hold' and recheck.json()['probe_made'] is False
    assert 'x-canary-mcp' not in contract['paths']['/api/sources/openalex/recheck']['post']
    extraction = client.get('/api/status').json()['pdf_extraction']
    assert extraction['concurrent_pdf_conversions'] == 1
    assert extraction['capacity_scope'] == 'service_data_directory'
    assert extraction['skip_superseded_copies'] is True
    compiled=client.post('/api/runs/compile',json={'spec':{'question':'Synthetic schema check?'}})
    validate_response(contract,'/api/runs/compile','post',200,compiled.json())
    started=client.post('/api/runs',json={'spec':{'question':'Synthetic schema check?'}})
    validate_response(contract,'/api/runs','post',201,started.json())
    for path in ('/api/runs/{key}','/api/runs/{key}/research','/api/runs/{key}/bundle'):
        response=client.get(path.replace('{key}','synthetic'))
        assert response.status_code==200,response.text
        validate_response(contract,path,'get',200,response.json())


@pytest.mark.parametrize('legacy', [True, False], ids=['legacy', 'new'])
def test_published_eligibility_stays_separate_from_screening(api, capsys, legacy):
    server, client, root = api
    question = 'Synthetic protocol?'
    assert client.post('/api/runs', json={'spec': {'question': question}}).status_code == 201
    workspace = root / 'synthetic'
    criteria = {'core': 'Measured timings. Theses and technical publications may qualify.',
                'supporting': 'Methods studies may qualify; measurements are optional.',
                'exclusion': 'Unrelated publications.',
                'full_paper_checks': ['Verify the reported endpoint.']}
    instruction = 'Label publication type and overlap.'
    plan = {'question': question, 'concepts': [{'name': 'activation timing', 'synonyms': [],
                                              'search_role': 'outcome'}],
            'outcomes': [], 'comparators': [], 'conditions': [], 'coverage_aspects': [],
            'queries': [{'id': 'Q1', 'search': 'activation timing', 'coverage_aspect_ids': [],
                         'purpose': '', 'collapse_reason': ''}], 'scope_note': '',
            'eligibility': criteria.copy()}
    if legacy:
        plan['eligibility']['core'] = criteria['core'].removesuffix('.') + '; label publication type and overlap.'
    else:
        plan.update(rationale='Resolve conflicting timing estimates.', other_variables=[], study_groups=[],
                    result_selection='Use all reported timings.', missing_information='Record missing timings.')
        plan['screening_instructions'] = [instruction, 'Record <unknown> when overlap cannot be established.']
    expected = dict(plan, eligibility=criteria,
                    screening_instructions=plan.get('screening_instructions', [instruction]))
    contract = client.get('/openapi.json').json()
    Draft202012Validator(contract['components']['schemas']['QuestionPlan']).validate(plan)
    assert coverage_runtime.validate_plan(plan) == plan
    if not legacy:
        Draft202012Validator(coverage_protocol.question_plan_schema()).validate(plan)
        with pytest.raises(ValueError, match='array of strings'):
            coverage_runtime.validate_plan(dict(plan, screening_instructions=instruction))
    raw_plan = workspace / 'question-plan.json'
    research_bundle.write_json(raw_plan, plan)
    original = raw_plan.read_bytes()
    payload_file = workspace / 'payload.json'
    payload = json.loads(payload_file.read_text())
    research_bundle.write_json(payload_file, dict(payload, question_plan=plan))
    result_path = '/api/runs/synthetic/bundle'
    query_net = workspace / 'query-net.json'
    if legacy:
        # Simulate a previously exported bundle before any re-export.
        research_bundle.write_json(query_net, plan)
        research_bundle.seal(workspace)
        assert client.get(result_path).raise_for_status().json()['research']['query_net'] == expected
        assert json.loads(query_net.read_text()) == plan
    coverage_export.export_bundle(workspace)
    research_bundle.seal(workspace)
    result = client.get(result_path).raise_for_status().json()
    validate_response(contract, '/api/runs/{key}/bundle', 'get', 200, result)
    assert result['research']['query_net'] == expected == json.loads(query_net.read_text())
    assert raw_plan.read_bytes() == original
    archive = client.get(result_path + '/archive').raise_for_status()
    with zipfile.ZipFile(io.BytesIO(archive.content)) as saved:
        assert saved.read('question-plan.json') == original
        assert json.loads(saved.read('query-net.json')) == expected
    assert cli.main(['bundle', 'synthetic', '--port', str(server.server_port)]) == 0
    assert json.loads(capsys.readouterr().out)['research']['query_net'] == expected

    contribution_path = '/api/runs/synthetic/exports/resolvent'
    contribution = client.get(contribution_path).raise_for_status().json()
    assert compact_bundle.payload(contribution)['question_plan'] == plan
    (workspace / '.run-reservation').touch()
    compact_bundle.retain(workspace)
    retained = client.get(result_path).raise_for_status().json()
    validate_response(contract, '/api/runs/{key}/bundle', 'get', 200, retained)
    assert retained['research']['query_net'] == expected
    retained_export = client.get(contribution_path).raise_for_status().json()
    assert compact_bundle.payload(retained_export)['question_plan'] == plan
    run_dashboard(root, result['research'], r"""
const html=coverageSelectionHtml(graph);
const criteria=html.match(/<summary>Selection criteria and coverage<\/summary>(.*?)<\/details>/s)[1];
assert.match(criteria,/Theses and technical publications may qualify\./);
assert.match(criteria,/Methods studies may qualify; measurements are optional\./);
assert.doesNotMatch(criteria,/publication type and overlap|Verify the reported endpoint/i);
assert.match(html,/<summary>Screening instructions<\/summary><ul><li>Label publication type and overlap\.<\/li>/);
assert.match(html,/<summary>Full-paper checks<\/summary><ul><li>Verify the reported endpoint\.<\/li>/);
if(graph.query_net.screening_instructions.length>1) assert.match(html,/Record &lt;unknown&gt;/);
const unannotated=coverageSelectionHtml({query_net:{eligibility:{core:'Measured timings.'}}});
assert.doesNotMatch(unannotated,/Screening instructions|Full-paper checks/);
""")


def test_mcp_lists_live_schemas_and_navigates_claim_to_original_over_api(api):
    server,client,_ = api
    async def scenario():
        parameters=StdioServerParameters(command=sys.executable,args=['-m','canary.cli','mcp','--port',str(server.server_port)])
        async with stdio_client(parameters) as (incoming,outgoing):
            async with ClientSession(incoming,outgoing) as session:
                initialized=await session.initialize()
                assert initialized.serverInfo.name=='canary'
                advertised=await session.list_tools()
                expected=tool_catalog(client.get('/openapi.json').json())
                assert {tool.name for tool in advertised.tools}==set(expected)
                for tool in advertised.tools:
                    assert tool.inputSchema==expected[tool.name]['tool'].inputSchema
                status=await session.call_tool('canary_status',{})
                assert not status.isError and status.structuredContent['service']=='available'
                connection=await session.call_tool('canary_connection',{})
                assert not connection.isError and connection.structuredContent['execution']=='service_host'
                compiled=await session.call_tool('canary_compile',{'spec':{'question':'Synthetic MCP fixture?'}})
                assert not compiled.isError
                assert compiled.structuredContent['resolved']['research']['effort']=='xhigh'
                spec = {'question': 'Synthetic MCP fixture?', 'study_brief': 'Compare two procedures.',
                        'retrieval': {'selection': 'citation_graph'}}
                started = await session.call_tool('canary_ask', {'spec': spec})
                assert not started.isError,started.content
                key=started.structuredContent['key']
                resolved = client.get(f'/api/runs/{key}/bundle').raise_for_status().json()['bundle']['specification']['resolved']
                assert resolved['study_brief'] == spec['study_brief']
                assert resolved['retrieval']['selection'] == 'citation_graph'
                compact=await session.call_tool('canary_export',{'key':key,'connector':'resolvent'})
                expanded=await session.call_tool('canary_export',{'key':key,'connector':'resolvent','representation':'expanded'})
                assert not compact.isError and not expanded.isError
                assert bundle_encoding.expand(compact.structuredContent) == expanded.structuredContent
                research=await session.call_tool('canary_research',{'key':key})
                assert not research.isError,research.content
                graph=research.structuredContent['research']['answer']
                claim=graph['claims'][0]
                assert claim['uncertainty']['class']=='moderate'
                assert claim['freshness']['policy']=='unknown'
                evidence=next(e for e in graph['evidence'] if e['id'] in claim['evidence_ids'])
                passage=next(p for p in graph['passages'] if p['id']==evidence['paragraph_id'])
                context=await session.call_tool('canary_source_context',{'key':key,'source_id':passage['source_id'],
                    'paragraph_id':passage['id'],'scope':'section'})
                assert not context.isError,context.content
                source=context.structuredContent
                assert source['offset_verified'] is True
                assert any(p['selected'] and p['text']==evidence['quote'] for p in source['paragraphs'])
                original=client.get(source['local_file_url']).text
                assert original[passage['start']:passage['end']]==evidence['quote']
                failed=await session.call_tool('canary_run',{'key':'missing'})
                assert failed.isError and failed.structuredContent['error']
                invalid=await session.call_tool('canary_cancel',{})
                assert invalid.isError
    asyncio.run(scenario())


def test_browser_origin_is_opt_in_and_does_not_expose_auth_controls(api,monkeypatch):
    _,client,_=api
    headers={'Origin':'http://localhost:3000','Sec-Fetch-Site':'cross-site'}
    assert client.post('/api/runs/compile',json={'spec':{'question':'q?'}},headers=headers).status_code==403
    monkeypatch.setenv('CANARY_ALLOWED_ORIGINS','http://localhost:3000')
    preflight=client.options('/api/runs/compile',headers=dict(headers,**{'Access-Control-Request-Method':'POST'}))
    assert preflight.status_code==204 and preflight.headers['access-control-allow-origin']==headers['Origin']
    response=client.post('/api/runs/compile',json={'spec':{'question':'q?'}},headers=headers)
    assert response.status_code==200 and response.headers['access-control-allow-origin']==headers['Origin']
    assert client.post('/api/auth/login',json={},headers=headers).status_code==403
    assert 'access-control-allow-origin' not in client.get('/api/auth/status',headers=headers).headers
    assert client.post('/api/runs/compile',json={'spec':{'question':'q?'}},headers={'Host':'canary:8789'}).status_code==403
    monkeypatch.setenv('CANARY_ALLOWED_HOSTS','canary:8789')
    assert client.post('/api/runs/compile',json={'spec':{'question':'q?'}},headers={'Host':'canary:8789'}).status_code==200


def test_remote_client_origin_is_explicit_and_missing_usage_stays_unknown(api,monkeypatch,capsys):
    server,client,_=api
    monkeypatch.setenv('CANARY_API_URL',f'http://127.0.0.1:{server.server_port}')
    assert cli.main(['runs','status'])==0
    assert json.loads(capsys.readouterr().out)['service']=='available'
    for value in ('https://user:secret@example.org','https://example.org/prefix','file:///tmp/data'):
        monkeypatch.setenv('CANARY_API_URL',value)
        with pytest.raises(ValueError):api_client.endpoint()
    runs.register_start('unknown','unknown','',path=server.RequestHandlerClass.supervisor.registry)
    assert client.get('/api/usage').json()['usage'][0]['tokens'] is None


def test_inspection_import_does_not_load_numerical_modeling_stack():
    subprocess.run([sys.executable,'-c','import sys; import canary.report; '
                    'assert "sklearn" not in sys.modules; assert "pandas" not in sys.modules'],check=True,timeout=10)
