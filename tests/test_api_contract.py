"""Verify the live contract and a real stdio MCP relay, with synthetic science."""
import asyncio
import json
from pathlib import Path
import sys
import subprocess
import threading

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from canary import api_client, api_contract, bundle_encoding, cli, coverage_export, dashboard, research_bundle, runs
from canary.mcp_server import tool_catalog
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
                started=await session.call_tool('canary_ask',{'spec':{'question':'Synthetic MCP fixture?'}})
                assert not started.isError,started.content
                key=started.structuredContent['key']
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
