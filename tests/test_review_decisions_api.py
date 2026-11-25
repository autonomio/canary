import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import jsonschema
import pytest

from canary import api_contract, cli, command_client, compact_bundle, dashboard, mcp_server, runs
from test_compact_bundle import prepared
from test_review_history import decision


def test_review_decisions_share_the_http_contract_across_clients(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / 'review'
    workspace.mkdir()
    prepared(workspace)
    registry = tmp_path / 'registry.json'
    row = runs.adopt_bundle(workspace, 'Saved review', '', registry)
    key = row['key']
    route = '/api/runs/' + key + '/review-decisions'

    def dispatch(request):
        handler = object.__new__(dashboard.Handler)
        handler.supervisor = SimpleNamespace(registry=registry)
        handler.server = SimpleNamespace(server_port=8789)
        handler.path = request.url.raw_path.decode()
        handler.headers = request.headers
        handler.rfile = io.BytesIO(request.content)
        handler._send = Mock()
        getattr(handler, 'do_' + request.method)()
        args = handler._send.call_args.args
        return httpx.Response(args[0], json=args[1])

    client = httpx.Client(base_url='http://127.0.0.1:8789', transport=httpx.MockTransport(dispatch))
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
    client.close()
