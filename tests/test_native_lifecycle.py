"""Real isolated process trees, no provider calls or scientific acceptance claims."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import httpx
import pytest

from canary import dashboard, processes, runs


def eventually(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('Expected process/state transition did not complete')


def test_pid_birth_mismatch_never_kills_another_process():
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        assert processes.stop_tree({'pid': process.pid, 'identity': 'wrong-birth'}) is False
        assert process.poll() is None
    finally:
        process.terminate(); process.wait(5)


def test_restart_recovers_live_worker_and_cancels_descendants_in_separate_sessions(tmp_path, monkeypatch):
    worker = tmp_path/'worker.py'
    worker.write_text('''import json,os,pathlib,signal,subprocess,sys,time
w=pathlib.Path(os.environ['OUT']);w.mkdir(parents=True)
(w/'source.txt').write_text('Committed source must survive cancellation')
(w/'run.json').write_text(json.dumps({'kind':'question','status':'researching','execution_mode':'coverage-guided full-paper research'}))
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
(w/'child.json').write_text(json.dumps({'pid':child.pid}))
time.sleep(60)
''')
    original = subprocess.Popen
    def launch(argv, **kwargs):
        if 'env' not in kwargs: return original(argv, **kwargs)
        return original([sys.executable, str(worker)], **kwargs)
    monkeypatch.setattr(dashboard.subprocess, 'Popen', launch)
    monkeypatch.setattr(dashboard.CodexAuth, 'worker_environment', lambda self: {'CODEX_HOME': self.home})
    registry = tmp_path/'registry.json'
    before = dashboard.Supervisor(registry=registry, root=tmp_path)
    started = before.start_question('Synthetic lifecycle check?')
    key = started['key']; workspace = Path(started['bundle'])
    eventually(lambda: (workspace/'child.json').is_file())
    child = json.loads((workspace/'child.json').read_text())
    child['identity'] = processes.identity(child['pid'])
    server = dashboard.make_server(0, registry=registry, root=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}') as client:
            live = client.get('/api/runs/'+key).json()
            assert live['live'] and live['can_cancel']
            assert live['started_at']
            assert client.post('/api/runs/'+key+'/cancel', json={}).status_code == 202
            eventually(lambda: client.get('/api/runs/'+key).json()['status'] == 'cancelled')
            eventually(lambda: not processes.live(child))
            assert (workspace/'source.txt').read_text() == 'Committed source must survive cancellation'
    finally:
        processes.stop_tree(runs.get(key, registry)['worker'])
        if processes.live(child): os.kill(child['pid'], signal.SIGKILL)
        server.shutdown();server.server_close();thread.join(5)


def test_dead_native_worker_with_active_manifest_becomes_interrupted(tmp_path):
    workspace = tmp_path/'partial';workspace.mkdir()
    (workspace/'run.json').write_text(json.dumps({'kind':'question','status':'auditing',
        'execution_mode':'coverage-guided full-paper research'}))
    registry = tmp_path/'registry.json'
    runs.register_start('lost','lost','',kind='question',bundle=str(workspace),path=registry,
                        worker={'pid':99999999,'identity':'gone'})
    after = dashboard.Supervisor(registry=registry, root=tmp_path).reconcile()[0]
    assert after['status'] == 'interrupted'
    assert after['live'] is False and after['can_cancel'] is False


@pytest.mark.parametrize('backend',['codex_exec','shared_codex_app_server'])
@pytest.mark.parametrize('cancel_mode',['api','signal'])
def test_real_native_worker_seals_cancelled_bundle_and_retains_receipts_in_output(tmp_path, monkeypatch, backend, cancel_mode):
    fake = tmp_path/'codex'
    fake.write_text('''#!/usr/bin/env python3
import pathlib,sys,time,json,os
if '--version' in sys.argv:
    print('codex-fixture 0.162.0');raise SystemExit()
if sys.argv[1]=='app-server':
    current={}
    for line in sys.stdin:
        m=json.loads(line)
        if 'id' not in m: continue
        method=m['method'];params=m.get('params',{});result={}
        if method=='config/read':
            user={'developer_instructions':'ignored by exec'} if os.environ['CANARY_FIXTURE_BACKEND']=='codex_exec' else {}
            result={'config':{'cli_auth_credentials_store':'file'},'layers':[{'name':{'type':'user'},'config':user}]}
        elif method=='thread/start':
            current=params
            result={'thread':{'id':'fixture-thread'},'model':params['model'],'cwd':params['cwd'],'reasoningEffort':params['config']['model_reasoning_effort']}
        elif method=='turn/start':
            pathlib.Path(current['cwd'],'synthetic-committed.txt').write_text('Synthetic material; no scientific evidence.')
            result={'turn':{'id':'fixture-turn'}}
        elif method=='turn/interrupt':
            print(json.dumps({'method':'turn/completed','params':{'threadId':'fixture-thread','turn':{'id':'fixture-turn','status':'interrupted'}}}),flush=True)
        print(json.dumps({'id':m['id'],'result':result}),flush=True)
    raise SystemExit()
assert sys.argv[1]=='exec'
pathlib.Path('synthetic-committed.txt').write_text('Synthetic material; no scientific evidence.')
print(json.dumps({'type':'thread.started','thread_id':'fixture-thread'}),flush=True)
time.sleep(60)
''')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('CODEX_HOME', str(tmp_path/'fixture-auth'))
    monkeypatch.setenv('CANARY_FIXTURE_BACKEND', backend)
    server = dashboard.make_server(0, registry=tmp_path/'registry.json', root=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    key = None
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=20) as client:
            result = client.post('/api/runs',json={'spec':{'question':'Synthetic cancellation proof?'}})
            assert result.status_code == 201, result.text
            key = result.json()['key'];workspace = tmp_path/'container-out'/key
            eventually(lambda: (workspace/'synthetic-committed.txt').is_file())
            if cancel_mode=='api':
                assert client.post('/api/runs/'+key+'/cancel',json={}).status_code == 202
            else:
                os.kill(runs.get(key,server.RequestHandlerClass.supervisor.registry)['worker']['pid'],signal.SIGTERM)
            eventually(lambda: client.get('/api/runs/'+key).json()['status']=='cancelled')
            bundle = client.get('/api/runs/'+key+'/bundle').json()['bundle']
            assert bundle['state'] == 'finished'
            assert bundle['execution']['status'] == 'cancelled'
            output = client.get('/api/runs/'+key+'/exports/resolvent?representation=expanded').json()
            receipt = output['method_metadata']['canary_view']['profile']['roles']['research']
            assert receipt['thread_id']=='fixture-thread'
            assert receipt['backend']==backend
            assert receipt['exit_code'] != 0
            assert not (workspace/'synthetic-committed.txt').exists()
            assert not (workspace/'research-receipt.json').exists()
            assert server.RequestHandlerClass.supervisor.execution.status()['active_shared_sessions']==0
            assert client.get('/api/runs/'+key+'/bundle/archive').status_code==200
            assert {p.name for p in workspace.iterdir()}=={'resolvent.json','.resolvent.sha256'}
    finally:
        if key: processes.stop_tree(runs.get(key,server.RequestHandlerClass.supervisor.registry)['worker'])
        server.shutdown();server.server_close();thread.join(5)
