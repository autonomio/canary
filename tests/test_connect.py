"""Connect starts only the local service; authentication remains an HTTP action."""
import os
from pathlib import Path
import socket
import subprocess

import httpx

from canary import cli, connect_cli
from test_codex_auth import auth_server


def test_connect_bootstraps_a_real_service_and_reuses_it(auth_server, monkeypatch, capsys):
    _, _, home=auth_server
    (home/'fixture-account').write_text('fixture credential')
    monkeypatch.setenv('CANARY_DATA_DIR',str(home.parent/'boot-data'))
    monkeypatch.setenv('PYTHONPATH',str(Path(cli.__file__).resolve().parents[1]))
    for name in ('CANARY_RUNS_REGISTRY','CANARY_RESEARCH_ROOTS','CANARY_API_URL'):
        monkeypatch.delenv(name,raising=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    launched=[]
    real_popen=subprocess.Popen
    def launch(argv,**kwargs):
        process=real_popen(argv,**kwargs)
        if argv[1:3]==['-m','canary.cli']:launched.append(process)
        return process
    monkeypatch.setattr(connect_cli.subprocess,'Popen',launch)
    try:
        assert cli.main(['connect','--port',str(port)])==0
        assert 'Connected to Canary' in capsys.readouterr().out
        assert len(launched)==1 and launched[0].poll() is None
        assert cli.main(['connect','--port',str(port)])==0
        capsys.readouterr()
        assert len(launched)==1
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False) as client:
            assert client.get('/api/runs').json()=={'runs':[]}
        assert not (home/'login-started').exists()
    finally:
        for process in launched:
            process.terminate();process.wait(timeout=10)


def test_unreachable_remote_service_never_launches_a_local_worker(monkeypatch, capsys):
    monkeypatch.setenv('CANARY_API_URL','https://canary.example.test')
    def fail(*args,**kwargs):raise httpx.ConnectError('Synthetic offline service')
    monkeypatch.setattr(connect_cli,'request',fail)
    monkeypatch.setattr(connect_cli.subprocess,'Popen',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Remote bootstrap')))
    assert cli.main(['connect'])==1
    assert 'service host' in capsys.readouterr().err


def test_missing_codex_has_actionable_error_without_partial_startup(monkeypatch,capsys,tmp_path):
    monkeypatch.delenv('CANARY_API_URL',raising=False)
    monkeypatch.setenv('CANARY_DATA_DIR',str(tmp_path/'must-not-start'))
    def fail(*args,**kwargs):raise httpx.ConnectError('Synthetic offline service')
    monkeypatch.setattr(connect_cli,'request',fail)
    monkeypatch.setattr(connect_cli.shutil,'which',lambda _:None)
    assert cli.main(['connect'])==1
    assert 'Install it on the Canary host' in capsys.readouterr().err
    assert not (tmp_path/'must-not-start').exists()
