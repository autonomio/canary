"""The API indexes existing local runs without changing research or ownership."""
import hashlib
import json
import os
from pathlib import Path
import threading

import httpx
import jsonschema

from canary import api_contract, cli, dashboard, footprint, runs


def test_discovery_keeps_existing_ownership_indexes_same_named_runs_and_is_read_only(tmp_path,monkeypatch):
    extra=tmp_path/'history'
    for name in ('first/result','second/result'):
        p=extra/name;p.mkdir(parents=True)
        (p/'run.json').write_text(json.dumps({'kind':'question','question':'Historical question?',
            'execution_mode':'coverage-guided full-paper research','status':'in_progress'}))
    # A paper's own metadata is not a nested research run.
    paper=extra/'first/result/papers/data';paper.mkdir(parents=True)
    (paper/'run.json').write_text('{}')
    monkeypatch.setenv('CANARY_RESEARCH_ROOTS',str(extra)+os.pathsep+str(extra/'first'))
    registry=tmp_path/'registry.json'
    first=extra/'first/result'
    runs.register_start('owned','Preserve name','Preserve brief',bundle=str(first),path=registry,
                        worker={'pid':1,'identity':'recorded'},run_spec_path='preserve-spec')
    original=runs.get('owned',registry)
    source={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in extra.rglob('*') if p.is_file()}
    result=footprint.refresh(tmp_path,registry)
    assert result['registered_runs']==2 and len(result['last_scan']['added_keys'])==1
    assert runs.get('owned',registry)==original
    assert source=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in extra.rglob('*') if p.is_file()}
    assert footprint.refresh(tmp_path,registry)['last_scan']['added_keys']==[]
    (extra/'second/result/run.json').unlink();(extra/'second/result').rmdir()
    assert footprint.status(tmp_path,registry)['missing_bundles']==1
    assert len(runs.load(registry))==2


def test_http_cli_and_agent_contract_expose_same_scan_and_unknown_activity(tmp_path,monkeypatch,capsys):
    extra=tmp_path/'history';bundle=extra/'result';bundle.mkdir(parents=True)
    (bundle/'run.json').write_text(json.dumps({'kind':'question','question':'Historical?',
        'execution_mode':'coverage-guided full-paper research','status':'in_progress'}))
    monkeypatch.setenv('CANARY_RESEARCH_ROOTS',str(extra))
    server=dashboard.make_server(0,root=tmp_path,registry=tmp_path/'registry.json')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        assert cli.main(['runs','discover','--port',str(server.server_port)])==0
        discovered=json.loads(capsys.readouterr().out)
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False) as client:
            state=client.get('/api/footprint').json()
            assert state==discovered and state['registered_runs']==1
            contract=client.get('/openapi.json').json()
            jsonschema.Draft202012Validator({'$ref':'#/components/schemas/Footprint',**contract}).validate(state)
            assert contract['paths']['/api/footprint/refresh']['post']['x-canary-mcp']['name']=='canary_refresh_footprint'
            row=client.get('/api/runs').json()['runs'][0]
            assert row['status']=='activity_unverified' and row['recorded_status']=='in_progress'
            assert row['live'] is False and row['can_cancel'] is False and row['ended_at'] is None
            assert client.post('/api/footprint/refresh',json={'unexpected':True}).status_code==400
            assert client.get('/').status_code==200
    finally:
        server.shutdown();server.server_close();thread.join()
