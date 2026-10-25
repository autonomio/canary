import threading

import httpx

from canary import cli, dashboard, runtime_paths, service_status


def test_data_root_is_independent_of_package_and_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('CANARY_DATA_DIR', raising=False)
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg'))
    assert runtime_paths.data_root() == tmp_path / 'xdg/canary'
    monkeypatch.setenv('CANARY_DATA_DIR', str(tmp_path / 'research'))
    assert runtime_paths.data_root() == tmp_path / 'research'


def test_headless_service_exposes_runtime_and_cli_without_assets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(service_status.shutil, 'which', lambda name: None)
    server = dashboard.make_server(0, registry=tmp_path/'registry.json', root=tmp_path, dashboard_enabled=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}') as client:
            status = client.get('/api/status').json()
            assert status['service'] == 'available'
            assert status['codex'] == {'available': False, 'version': None}
            assert status['pdf_extraction']['engine'] == 'pypdf'
            assert status['dashboard_enabled'] is False
            assert client.get('/').status_code == 404
            assert client.get('/assets/research.css').status_code == 404
            assert client.get('/api/runs').json() == {'runs': []}
            assert cli.main(['runs', 'status', '--port', str(server.server_port)]) == 0
            assert 'Runtime availability only' in capsys.readouterr().out
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
