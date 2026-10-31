"""Exercise real stdio/HTTP ownership using an isolated fake Codex, never real credentials."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import threading
import signal
import sys
import time

import httpx
import pytest

from canary import cli, codex_auth, coverage_runtime, dashboard


@pytest.fixture
def auth_server(tmp_path, monkeypatch):
    home = tmp_path / 'credentials'
    home.mkdir()
    fake = tmp_path / 'codex'
    fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
if '--version' in sys.argv:
    print('codex-fixture 0.162.0');raise SystemExit()
home=pathlib.Path(os.environ['CODEX_HOME'])
credential=home/'fixture-account'
login_id=None
sent=False
counter=0
def send(v):
    print(json.dumps(v),flush=True)
for line in sys.stdin:
    m=json.loads(line); method=m.get('method'); result={}
    if method=='initialize':
        assert sys.argv[1:]==['app-server']
    elif method=='config/read':
        result={'config':{'cli_auth_credentials_store':(home/'store').read_text() if (home/'store').exists() else 'file','private':'DO-NOT-EXPOSE'}}
    elif method=='account/read':
        if (home/'stall').exists(): time.sleep(60)
        if login_id and (home/'complete').exists() and not sent:
            credential.write_text('fake secret, never exported'); sent=True
            send({'method':'account/login/completed','params':{'loginId':login_id,'success':True}})
        result={'account':{'type':'chatgpt','email':'private@example.com','accessToken':'DO-NOT-EXPOSE'} if credential.exists() else None,'requiresOpenaiAuth':True}
    elif method=='account/login/start':
        (home/'login-started').touch()
        while (home/'delay-start').exists(): time.sleep(.01)
        counter+=1; login_id='login-'+str(counter); sent=False
        if (home/'rpc-fail').exists():
            send({'id':m['id'],'error':{'message':'DO-NOT-EXPOSE'}});continue
        if (home/'early').exists():
            credential.write_text('fixture');sent=True
            send({'method':'account/login/completed','params':{'loginId':login_id,'success':not (home/'fail').exists(),'error':'DO-NOT-EXPOSE'}})
        if m['params']['type']=='chatgptDeviceCode':
            result={'type':'chatgptDeviceCode','loginId':login_id,'verificationUrl':'https://auth.openai.com/codex/device','userCode':'ABCD-1234'}
        else:
            result={'type':'chatgpt','loginId':login_id,'authUrl':'https://auth.openai.com/oauth/authorize?state=fixture'}
        if (home/'bad-url').exists():result['authUrl']='javascript:alert(1)'
    elif method=='account/logout':
        credential.unlink(missing_ok=True)
    elif method=='account/login/cancel':
        assert m['params']['loginId']==login_id
        if (home/'cancel-complete').exists():
            credential.write_text('fixture')
            send({'method':'account/login/completed','params':{'loginId':login_id,'success':True}})
            result={'status':'notFound'}
        else:result={'status':'canceled'}
    elif method=='initialized':continue
    else:raise AssertionError(method)
    send({'id':m['id'],'result':result})
''')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('CODEX_HOME', str(home))
    server = dashboard.make_server(0, root=tmp_path, registry=tmp_path/'registry.json')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', timeout=5, trust_env=False) as client:
        yield server, client, home
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_login_http_cli_restart_and_logout_share_persistent_context(auth_server, capsys):
    server, client, home = auth_server
    assert client.get('/api/auth/status').json()['state'] == 'signed_out'
    login = client.post('/api/auth/login', json={'method':'device'}).json()
    assert login['user_code'] == 'ABCD-1234'
    assert login['state'] == 'pending'
    assert client.post('/api/auth/login', json={'method':'browser'}).json() == login
    (home/'complete').touch()
    progress = client.get('/api/auth/login/'+login['id'])
    assert progress.headers['cache-control'] == 'no-store'
    assert progress.json() == dict(login, state='complete', url=None, user_code=None)
    assert cli.main(['auth','status','--port',str(server.server_port),'--json']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['authenticated'] and status['mode']=='chatgpt'
    # The operator may see which account is connected; tokens never leave the CLI.
    assert status['email']=='private@example.com'
    assert 'DO-NOT-EXPOSE' not in json.dumps(status)
    auth = server.RequestHandlerClass.supervisor.auth
    old_proc = auth.proc
    server.server_close()
    assert old_proc.poll() is not None
    # A new service process uses native stored credentials, not a Canary cache.
    restarted = codex_auth.CodexAuth(str(home))
    try:
        assert restarted.status()['authenticated']
        signed_out = restarted.logout()
        assert signed_out['state']=='signed_out' and signed_out['email'] is None
    finally:
        restarted.close()
    assert not (home/'fixture-account').exists()


def test_browser_cancel_unknown_login_and_concurrent_reuse(auth_server):
    _, client, _ = auth_server
    with ThreadPoolExecutor(max_workers=3) as pool:
        logins = list(pool.map(lambda _: client.post('/api/auth/login', json={}).json(), range(3)))
    assert len({login['id'] for login in logins})==1
    login = logins[0]
    assert login['url'].startswith('https://auth.openai.com/') and login['user_code'] is None
    assert client.post('/api/auth/login/'+login['id']+'/cancel', json={}).json()['state']=='cancelled'
    assert client.get('/api/auth/login/missing').status_code==404
    assert client.post('/api/auth/login', json={}).json()['id'] != login['id']


@pytest.mark.parametrize('failed', [False, True])
def test_completion_before_login_response_is_preserved_and_redacted(auth_server, failed):
    _, client, home = auth_server
    (home/'early').touch()
    if failed: (home/'fail').touch()
    login = client.post('/api/auth/login', json={}).json()
    assert login['state']==('failed' if failed else 'complete')
    assert login['url'] is None and login['user_code'] is None
    assert 'DO-NOT-EXPOSE' not in json.dumps(login)


@pytest.mark.parametrize('marker', ['rpc-fail', 'bad-url'])
def test_provider_errors_and_unsafe_challenges_never_escape(auth_server, marker):
    _, client, home = auth_server
    (home/marker).touch()
    response = client.post('/api/auth/login', json={})
    assert response.status_code >= 500
    assert 'DO-NOT-EXPOSE' not in response.text and 'javascript:' not in response.text


def test_missing_codex_and_timeout_are_truthful_and_cleanup_process(auth_server, monkeypatch):
    server, client, home = auth_server
    auth = server.RequestHandlerClass.supervisor.auth
    with monkeypatch.context() as patch:
        patch.setattr(codex_auth.shutil, 'which', lambda _: None)
        status = client.get('/api/auth/status').json()
        assert status['state']=='unavailable' and not status['available']
        assert client.post('/api/auth/login', json={}).status_code==503
    assert auth.status()['available']
    auth.timeout = .15
    proc=auth.proc
    (home/'stall').touch()
    assert auth.status()['state']=='unavailable'
    assert proc.poll() is not None


def test_auth_http_access_and_validation(auth_server):
    _, client, _ = auth_server
    for headers in ({'Origin':'https://attacker.test'}, {'Origin':'null'}, {'Host':'attacker.test'}, {'Sec-Fetch-Site':'cross-site'}):
        assert client.post('/api/auth/login',json={},headers=headers).status_code==403
    assert client.post('/api/auth/login',content='{}').status_code==415
    for raw in ('[]','null','{', '{"method":"other"}', '{"token":"secret"}'):
        assert client.post('/api/auth/login',content=raw,headers={'Content-Type':'application/json'}).status_code==400
    assert client.get('/api/auth/status',headers={'Origin':str(client.base_url).rstrip('/')}).status_code==200


def test_cli_login_and_interrupt_use_only_http(auth_server, monkeypatch, capsys):
    server, _, home = auth_server
    from canary import auth_cli
    opened=[]
    monkeypatch.setattr(auth_cli.webbrowser,'open',opened.append)
    from types import SimpleNamespace
    monkeypatch.setattr(auth_cli, 'time', SimpleNamespace(sleep=lambda _: (home/'complete').touch()))
    assert cli.main(['auth','login','--port',str(server.server_port)])==0
    assert opened and 'signed in' in capsys.readouterr().out
    assert cli.main(['auth','logout','--port',str(server.server_port)])==0
    (home/'complete').unlink()
    def interrupt(_):raise KeyboardInterrupt
    monkeypatch.setattr(auth_cli, 'time', SimpleNamespace(sleep=interrupt))
    assert cli.main(['auth','login','--device','--port',str(server.server_port)])==130
    assert server.RequestHandlerClass.supervisor.auth.login['state']=='cancelled'


def test_worker_uses_api_owned_home_and_native_file_store(auth_server, monkeypatch):
    server, _, home = auth_server
    sup=server.RequestHandlerClass.supervisor
    captured={}
    real_popen=subprocess.Popen
    def worker(argv, **kwargs):
        if 'app-server' in argv or 'env' not in kwargs: return real_popen(argv, **kwargs)
        captured.update(kwargs['env'])
        return real_popen(['true'],**kwargs)
    monkeypatch.setattr(dashboard.subprocess,'Popen',worker)
    monkeypatch.setenv('CODEX_HOME', '/must-not-override-service-context')
    sup.start_question('Synthetic question?')
    assert captured['CODEX_HOME']==str(home)
    assert captured['CANARY_CODEX_CREDENTIALS_STORE']=='file'
    assert captured['CANARY_CODEX_EXECUTABLE']==str(home.parent/'codex')
    command=coverage_runtime.command_line('codex','sol','xhigh','workspace','schema','output')
    assert 'cli_auth_credentials_store="file"' in command


@pytest.mark.parametrize('store', ['file', 'keyring', 'auto'])
def test_connect_reuses_existing_login_and_pins_store_for_both_roles(auth_server, capsys, monkeypatch, store):
    server, client, home = auth_server
    (home/'store').write_text(store)
    (home/'fixture-account').write_text('fixture')
    assert cli.main(['connect','--port',str(server.server_port)]) == 0
    output=capsys.readouterr().out
    assert 'Connected to Canary' in output and store+' credentials' in output
    assert 'Checked when research starts' in output
    assert not (home/'login-started').exists()
    context=server.RequestHandlerClass.supervisor.auth.worker_environment()
    assert context['CANARY_CODEX_CREDENTIALS_STORE']==store
    monkeypatch.setenv('CANARY_CODEX_CREDENTIALS_STORE',context['CANARY_CODEX_CREDENTIALS_STORE'])
    for model,effort in [('gpt-6.1-sol','xhigh'),('gpt-6-astra','ultra')]:
        command=coverage_runtime.command_line(context['CANARY_CODEX_EXECUTABLE'],model,effort,'workspace','schema','out')
        assert 'cli_auth_credentials_store="'+store+'"' in command
    receipt=client.get('/api/connection')
    assert receipt.headers['cache-control']=='no-store'
    assert receipt.json()['credential_store']==store
    assert receipt.json()['execution']=='service_host'
    assert 'DO-NOT-EXPOSE' not in receipt.text and 'private@example.com' not in receipt.text


def test_connect_signs_in_once_then_reuses_the_same_account(auth_server, monkeypatch, capsys):
    server, client, home=auth_server
    from canary import auth_cli
    from types import SimpleNamespace
    monkeypatch.setattr(auth_cli, 'time', SimpleNamespace(sleep=lambda _: (home/'complete').touch()))
    assert cli.main(['connect','--device','--port',str(server.server_port)])==0
    assert 'ABCD-1234' in capsys.readouterr().out
    login=server.RequestHandlerClass.supervisor.auth.login
    assert cli.main(['connect','--port',str(server.server_port)])==0
    assert 'Code:' not in capsys.readouterr().out
    assert server.RequestHandlerClass.supervisor.auth.login==login


def test_ephemeral_login_cannot_be_misreported_as_a_reusable_connection(auth_server):
    _,client,home=auth_server
    (home/'store').write_text('ephemeral')
    receipt=client.get('/api/connection').json()
    assert not receipt['auth']['available'] and receipt['credential_store'] is None
    assert 'ephemeral' in receipt['auth']['error']


def test_read_only_connection_never_reveals_a_pending_challenge(auth_server):
    _,client,_=auth_server
    assert client.post('/api/auth/login',json={'method':'device'}).json()['user_code']=='ABCD-1234'
    receipt=client.get('/api/connection')
    assert receipt.json()['auth']['login'] is None
    assert 'ABCD-1234' not in receipt.text and 'https://auth.openai.com/' not in receipt.text


def test_cancel_racing_completion_does_not_claim_it_cancelled(auth_server):
    _, client, home = auth_server
    login=client.post('/api/auth/login', json={}).json()
    (home/'cancel-complete').touch()
    result=client.post('/api/auth/login/'+login['id']+'/cancel', json={}).json()
    assert result['state']=='complete'
    assert client.get('/api/auth/status').json()['authenticated']


def test_missing_credential_directory_is_created_privately(auth_server):
    server, client, home = auth_server
    home.rmdir()
    assert client.get('/api/auth/status').json()['state']=='signed_out'
    assert home.is_dir() and home.stat().st_mode & 0o777 == 0o700


def test_sigint_during_initial_http_post_cancels_the_created_flow(auth_server):
    server, client, home = auth_server
    (home/'delay-start').touch()
    process=subprocess.Popen([sys.executable, '-m', 'canary.cli', 'auth', 'login', '--device',
                              '--port', str(server.server_port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline=time.monotonic()+15
        while not (home/'login-started').exists():
            assert process.poll() is None
            assert time.monotonic()<deadline
            time.sleep(.01)
        process.send_signal(signal.SIGINT)
        (home/'delay-start').unlink()
        stdout,stderr=process.communicate(timeout=10)
        assert process.returncode==130, (stdout,stderr)
        assert client.get('/api/auth/status').json()['login']['state']=='cancelled'
        assert 'could not be confirmed' not in stderr
    finally:
        if process.poll() is None:
            process.kill();process.wait()


def test_completion_after_account_snapshot_refreshes_the_dashboard_state(auth_server, monkeypatch):
    server, client, home = auth_server
    login=client.post('/api/auth/login', json={}).json()
    auth=server.RequestHandlerClass.supervisor.auth
    rpc=auth._rpc
    def snapshot_then_completion(method, params=None):
        result=rpc(method,params)
        if method=='account/read' and result['account'] is None:
            (home/'fixture-account').write_text('fixture')
            auth.messages.put({'method':'account/login/completed','params':{'loginId':login['id'],'success':True}})
        return result
    monkeypatch.setattr(auth,'_rpc',snapshot_then_completion)
    status=client.get('/api/auth/status').json()
    assert status['state']=='signed_in' and status['authenticated']
    assert status['login']['state']=='complete'


def test_default_http_port_accepts_browser_normalization_without_other_origins(auth_server):
    server, client, _ = auth_server
    actual_port=server.server_port
    server.server_port=80
    try:
        for host in ('localhost','localhost:80','127.0.0.1','127.0.0.1:80'):
            origin='http://'+host.removesuffix(':80')
            assert client.get('/api/auth/status',headers={'Host':host,'Origin':origin}).status_code==200
            assert client.get('/api/auth/status',headers={'Host':host,'Origin':origin+':80'}).status_code==200
        for origin in ('http://localhost:81','https://localhost','http://attacker.test'):
            assert client.post('/api/auth/login',json={},headers={'Host':'localhost','Origin':origin}).status_code==403
    finally:
        server.server_port=actual_port
