import io
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import jsonschema
import pytest

from canary import api_contract, cli, command_client, compact_bundle, dashboard, mcp_server, review_history, runs
from test_compact_bundle import prepared
from test_review_history import decision


@pytest.fixture
def review_client(tmp_path):
    registry = tmp_path / 'registry.json'
    root = tmp_path / 'service'

    def dispatch(request):
        handler = object.__new__(dashboard.Handler)
        handler.supervisor = SimpleNamespace(registry=registry, root=root)
        handler.server = SimpleNamespace(server_port=8789)
        handler.path = request.url.raw_path.decode()
        handler.headers = request.headers
        handler.rfile = io.BytesIO(request.content)
        handler._send = Mock()
        handler._send_file = lambda path, kind: handler._send(200, Path(path).read_bytes())
        getattr(handler, 'do_' + request.method)()
        status, data = handler._send.call_args.args[:2]
        return httpx.Response(status, content=data) if isinstance(data, bytes) else httpx.Response(status, json=data)

    with httpx.Client(base_url='http://127.0.0.1:8789', transport=httpx.MockTransport(dispatch)) as client:
        yield client, registry, root, dispatch


def test_review_decisions_share_the_http_contract_across_clients(tmp_path, monkeypatch, capsys, review_client):
    client, registry, root, dispatch = review_client
    workspace = tmp_path / 'review'
    workspace.mkdir()
    prepared(workspace)
    row = runs.adopt_bundle(workspace, 'Saved review', '', registry)
    key = row['key']
    route = '/api/runs/' + key + '/review-decisions'
    msg = {'request_id':'first', 'expected_last_entry':'initial', 'decision':decision()}
    contract = api_contract.document()
    catalog = mcp_server.tool_catalog(contract)
    write = catalog['canary_record_review_decision']
    read = catalog['canary_review_edition']
    jsonschema.validate(dict(key=key, **msg), write['tool'].inputSchema)
    for bad_id in ('two words', 'trailing\n'):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(dict(key=key, **dict(msg, request_id=bad_id)), write['tool'].inputSchema)
    assert write['path'].replace('{key}', key) == read['path'].replace('{key}', key) == route
    assert client.get(route).json()['last_entry'] == 'initial'
    saved = client.post(route, json=msg)
    assert saved.status_code == 200
    receipt = saved.json()
    jsonschema.validate(receipt, write['tool'].outputSchema)
    edition = client.get(route, params=receipt).json()
    jsonschema.validate(edition, read['tool'].outputSchema)
    retry = client.post(route, json=msg)
    assert retry.json() == receipt
    conflict = client.post(route, json=dict(msg, request_id='another'))
    assert conflict.status_code == 409 and conflict.json()['code'] == 'review_conflict'
    assert not (workspace / review_history.NAME).exists()
    with zipfile.ZipFile(io.BytesIO(client.get('/api/runs/' + key + '/bundle/archive').content)) as archive:
        assert json.loads(archive.read('review-decisions.json')) == edition
    compact_bundle.retain(workspace)
    for malformed in (None, [], 'text', {}, dict(msg, request_id='two words'), dict(msg, decision={}),
                      dict(msg, decision=decision(decided_by={'kind':'software', 'name':'Extractor'}))):
        assert client.post(route, content=json.dumps(malformed), headers={'Content-Type':'application/json'}).status_code == 400
    assert client.get(route + '?last_entry=absent').status_code == 404
    assert client.post(route, json={'decision': {}}).status_code == 400
    assert client.get('/api/runs/absent/review-decisions').status_code == 404

    path = tmp_path / 'correction.json'
    path.write_text(json.dumps({'request_id':'correction', 'expected_last_entry':edition['last_entry'],
                                'decision':decision('Corrected result'), 'replaces':edition['last_entry']}))
    def client_factory(**kwargs):
        return httpx_client(transport=httpx.MockTransport(dispatch), **kwargs)
    httpx_client = httpx.Client
    monkeypatch.setattr(command_client.httpx, 'Client', client_factory)
    assert cli.main(['decisions', key, '--submit', str(path)]) == 0
    correction = client.get(route, params=json.loads(capsys.readouterr().out)).json()
    assert correction['entries'][-1]['replaces'] == edition['last_entry']
    assert cli.main(['decisions', key, '--edition', edition['last_entry']]) == 0
    assert json.loads(capsys.readouterr().out)['entries'] == edition['entries']
    workspace.rename(tmp_path / 'moved-review')
    assert client.get(route).status_code == 404
    assert client.post(route, json=msg).status_code == 404


def test_owned_run_uses_local_history_and_reports_storage_errors(review_client):
    client, registry, root, _ = review_client
    workspace = root / 'container-out' / 'owned'
    workspace.mkdir(parents=True)
    prepared(workspace)
    runs.register_start('owned', 'Owned', '', bundle=str(workspace), run_spec_path='run.json', path=registry)
    route = '/api/runs/owned/review-decisions'
    msg = dict(request_id='first', expected_last_entry='initial', decision=decision())
    saved = client.post(route, json=msg)
    assert saved.status_code == 200
    path = workspace / review_history.NAME
    assert path.exists()
    for size in (65536, 65537):
        skeleton = json.dumps(dict(msg, request_id='size-test', expected_last_entry=saved.json()['last_entry']))
        body = skeleton.ljust(size)
        response = client.post(route, content=body)
        assert response.status_code == (200 if size == 65536 else 413)
        if size == 65537:
            assert response.json()['code'] == 'decision_too_large'
    with sqlite3.connect(path) as db:
        db.execute("UPDATE entries SET body = replace(body, 'Included', 'Changed')")
    for response in (client.get(route), client.post(route, json=msg), client.get('/api/runs/owned/bundle/archive')):
        assert response.status_code == 500
        assert response.json()['code'] == 'review_storage_error'


def test_adoption_preserves_existing_history_and_reads_old_registry_rows(tmp_path, review_client):
    client, registry, root, _ = review_client
    original = root / 'container-out' / 'historical'
    original.mkdir(parents=True)
    prepared(original)
    first = review_history.append(original, decision(), request_id='original', expected_last_entry='initial')
    row = runs.adopt_bundle(original, path=registry)
    row.pop('adopted')
    runs.upsert(row, registry)
    snapshot = (original / review_history.NAME).read_bytes()
    route = '/api/runs/historical/review-decisions'
    assert client.get(route).json()['last_entry'] == first
    msg = dict(request_id='next', expected_last_entry=first, replaces=first, decision=decision('Revised'))
    second = client.post(route, json=msg).json()['last_entry']
    assert (original / review_history.NAME).read_bytes() == snapshot
    assert review_history.edition(original)['last_entry'] == first
    with zipfile.ZipFile(io.BytesIO(client.get('/api/runs/historical/bundle/archive').content)) as archive:
        assert json.loads(archive.read('review-decisions.json'))['last_entry'] == second
    moved = tmp_path / 'renamed'
    original.rename(moved)
    runs.adopt_bundle(moved, path=registry)
    assert client.get('/api/runs/renamed/review-decisions').json()['last_entry'] == second


def test_missing_base_metadata_and_database_errors_are_server_errors(tmp_path, review_client, monkeypatch):
    from canary import research_bundle
    client, registry, root, _ = review_client
    workspace = root / 'container-out' / 'saved'
    workspace.mkdir(parents=True)
    prepared(workspace)
    runs.register_start('saved', 'Saved', '', bundle=str(workspace), run_spec_path='run.json', path=registry)
    route = '/api/runs/saved/review-decisions'
    msg = dict(request_id='first', expected_last_entry='initial', decision=decision())
    assert client.post(route, json=msg).status_code == 200
    assert client.get(route, params={'last_entry':'absent'}).json()['code'] == 'unknown_review_edition'
    assert client.post(route, content=b'{').json()['code'] == 'invalid_json'
    chunked = client.post(route, content=b'{}', headers={'Transfer-Encoding':'chunked'})
    assert chunked.status_code == 411 and chunked.json()['code'] == 'length_required'
    with monkeypatch.context() as patch:
        patch.setattr(research_bundle, 'read', lambda root: {})
        assert client.get(route, params={'last_entry':'absent'}).status_code == 500
    def locked_db(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(sqlite3, 'connect', locked_db)
    for response in (client.get(route), client.post(route, json=msg)):
        assert response.status_code == 500 and response.json()['code'] == 'review_storage_error'


def test_incomplete_import_is_retried_from_source(tmp_path, review_client, monkeypatch):
    import shutil
    client, registry, root, _ = review_client
    source = tmp_path / 'history'
    source.mkdir()
    prepared(source)
    first = review_history.append(source, decision(), request_id='original', expected_last_entry='initial')
    runs.adopt_bundle(source, path=registry)
    route = '/api/runs/history/review-decisions'
    copyfile = shutil.copyfile
    def change_after_copy(src, dst):
        copyfile(src, dst)
        replacement = source / 'replacement'
        replacement.write_bytes(src.read_bytes())
        replacement.replace(src)
    with monkeypatch.context() as patch:
        patch.setattr(shutil, 'copyfile', change_after_copy)
        assert client.get(route).status_code == 500
    assert not list((root / 'review-decisions').rglob(review_history.NAME))
    assert client.get(route).json()['last_entry'] == first


def test_relative_registry_path_points_to_the_same_archive_and_edition(tmp_path, review_client, monkeypatch):
    client, registry, _, _ = review_client
    workspace = tmp_path / 'historical'
    workspace.mkdir()
    prepared(workspace)
    row = runs.adopt_bundle(workspace, path=registry)
    monkeypatch.chdir(tmp_path)
    runs.upsert(dict(row, bundle='historical'), registry)
    route = '/api/runs/historical'
    head = client.post(route + '/review-decisions',
                       json=dict(request_id='one', expected_last_entry='initial', decision=decision())).json()
    with zipfile.ZipFile(io.BytesIO(client.get(route + '/bundle/archive').content)) as archive:
        assert json.loads(archive.read('review-decisions.json'))['last_entry'] == head['last_entry']


def test_archive_distinguishes_storage_for_decisions_and_for_the_download(tmp_path, review_client, monkeypatch):
    client, registry, root, _ = review_client
    source = tmp_path / 'archive'
    source.mkdir()
    prepared(source)
    runs.adopt_bundle(source, path=registry)
    route = '/api/runs/archive/bundle/archive'
    root.mkdir()
    (root / 'review-decisions').write_text('occupied')
    response = client.get(route)
    assert response.status_code == 500 and response.json()['code'] == 'review_storage_error'
    def disk_full(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(zipfile.ZipFile, 'write', disk_full)
    response = client.get(route)
    assert response.status_code == 500 and response.json()['code'] == 'archive_storage_error'


def test_reading_an_empty_import_does_not_hide_a_later_source_history(tmp_path, review_client):
    client, registry, _, _ = review_client
    source = tmp_path / 'empty'
    source.mkdir()
    prepared(source)
    runs.adopt_bundle(source, path=registry)
    route = '/api/runs/empty/review-decisions'
    assert client.get(route).json()['entries'] == []
    first = review_history.append(source, decision(), request_id='first', expected_last_entry='initial')
    assert client.get(route).json()['last_entry'] == first


def test_import_includes_committed_wal_frames_and_keeps_the_source_read_only(tmp_path, review_client):
    from contextlib import closing
    client, registry, _, _ = review_client
    source = tmp_path / 'wal'
    source.mkdir()
    prepared(source)
    first = review_history.append(source, decision(), request_id='one', expected_last_entry='initial')
    path = source / review_history.NAME
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM history').fetchall()
        last = review_history.append(source, decision('Later evidence'), request_id='two', expected_last_entry=first)
        files = [path, path.with_name(path.name + '-wal'), path.with_name(path.name + '-shm')]
        before = [file.read_bytes() for file in files]
        runs.adopt_bundle(source, path=registry)
        data = client.get('/api/runs/wal/review-decisions').json()
        assert [entry['id'] for entry in data['entries']] == [first, last]
        assert before == [file.read_bytes() for file in files]


def test_linked_output_directory_keeps_native_decisions_in_the_run(tmp_path, review_client):
    client, registry, root, _ = review_client
    output = tmp_path / 'external-output'
    output.mkdir()
    root.mkdir()
    (root / 'container-out').symlink_to(output, target_is_directory=True)
    workspace = output / 'owned'
    workspace.mkdir()
    prepared(workspace)
    runs.register_start('owned', 'Owned', '', bundle=str(workspace), run_spec_path='run.json', path=registry)
    msg = dict(request_id='first', expected_last_entry='initial', decision=decision())
    assert client.post('/api/runs/owned/review-decisions', json=msg).status_code == 200
    assert (workspace / review_history.NAME).exists()
