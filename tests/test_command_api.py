"""Public CLI boundaries exercised over HTTP; scientific engines remain separate."""
import json
from pathlib import Path
import threading

import httpx
import pytest

from canary import cli, dashboard, research_tools, runs, service_commands, tools_cli


@pytest.fixture
def api(tmp_path):
    server = dashboard.make_server(0, root=tmp_path, registry=tmp_path/'registry.json')
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', timeout=20) as client:
        yield server, client, tmp_path
    server.shutdown();server.server_close();thread.join(5)


def test_ask_is_compiled_by_api_and_client_never_launches_science(api, monkeypatch, capsys):
    server, client, root = api
    captured = []
    def start(packet, name='', brief=''):
        captured.append(packet);return {'key':'api-owned'}
    monkeypatch.setattr(server.RequestHandlerClass.supervisor, 'start_compiled', start)
    monkeypatch.setattr(cli, 'review', lambda *a, **k: pytest.fail('Client invoked a provider workflow'))
    assert cli.main(['ask','A new question?','--port',str(server.server_port)]) == 0
    assert json.loads(capsys.readouterr().out)['key'] == 'api-owned'
    assert captured[0]['resolved']['retrieval']['target_papers'] == 50
    assert captured[0]['resolved']['research']['effort'] == 'xhigh'
    assert not (root/'container-out').exists()


def test_public_registry_commands_only_use_api_and_import_preserves_ownership(api, monkeypatch, capsys):
    server, client, root = api
    source = root/'historical';source.mkdir()
    (source/'manifest.json').write_text(json.dumps({'kind':'review','status':'complete','spec':{'question':'Synthetic?'}}))
    port = str(server.server_port)
    monkeypatch.setenv('CANARY_RUNS_REGISTRY', '/not-a-client-data-source')
    assert cli.main(['runs','adopt','--bundle',str(source),'--port',port]) == 0
    key = source.name
    assert 'adopted '+key in capsys.readouterr().out
    for argv in (['runs','list'],['runs','show',key],['runs','log',key],['inspect',key]):
        assert cli.main(argv+['--port',port]) == 0
        capsys.readouterr()
    registry = server.RequestHandlerClass.supervisor.registry
    row = runs.get(key, registry);row['worker'] = {'pid':123,'identity':'preserve'};runs.upsert(row, registry)
    assert client.post('/api/runs/import',json={'server_path':str(source)}).json()['worker'] == row['worker']
    assert runs.get(key, registry)['worker'] == row['worker']


def test_csv_upload_and_async_legacy_submission_use_server_inputs(api, tmp_path, monkeypatch, capsys):
    server, client, root = api
    source = tmp_path/'client.csv';source.write_text('x,target\n1,0\n2,1\n')
    received = []
    def start(body):
        received.append(body)
        normalized = service_commands.prepare(body['command'],body['options'],root,server.RequestHandlerClass.supervisor.registry)
        assert Path(normalized['csv']).read_bytes() == source.read_bytes()
        return {'key':'analysis-from-upload'}
    monkeypatch.setattr(server.RequestHandlerClass.supervisor,'start_action',start)
    assert cli.main(['analyze',str(source),'--target','target','--port',str(server.server_port)]) == 0
    assert received[0]['options']['csv'].startswith('input:')
    assert str(source) not in json.dumps(received)
    assert json.loads(capsys.readouterr().out)['key'] == 'analysis-from-upload'


def test_invalid_compatibility_fields_never_start_worker(api):
    _, client, _ = api
    for body in ({'command':'shell','options':{}}, {'command':'review','options':{'question':'x','execute':'bad'}},
                 {'command':'review','options':{'question':[]}}):
        assert client.post('/api/actions',json=body).status_code == 400


def test_reading_finished_source_through_tool_api_does_not_change_bundle(api, capsys):
    server, client, root = api
    source = root/'synthetic';(source/'passages').mkdir(parents=True)
    paragraph = {'id':'W1-p1','reference_id':'W1','page':1,'start':0,'end':18,'text':'Synthetic excerpt.'}
    (source/'passages/W1.jsonl').write_text(json.dumps(paragraph)+'\n')
    registry = server.RequestHandlerClass.supervisor.registry
    runs.register_start('synthetic','synthetic','',bundle=str(source),status='done',path=registry)
    assert tools_cli.main(['--run','synthetic','--port',str(server.server_port),'source','W1']) == 0
    assert json.loads(capsys.readouterr().out) == [paragraph]
    assert not (source/'source-reads.jsonl').exists()
    assert client.post('/api/runs/synthetic/tools/acquire',json={'ids':['W1']}).status_code == 400


def test_legacy_worker_copies_input_before_engine_writes(tmp_path, monkeypatch):
    source = tmp_path/'original';source.mkdir();(source/'note.txt').write_text('original')
    target = tmp_path/'new-action'
    def engine(argv):
        assert argv[0] == 'assess'
        run_dir = Path(argv[argv.index('--run-dir')+1]);assert run_dir == target
        (run_dir/'note.txt').write_text('new assessment')
        return 0
    monkeypatch.setattr(cli,'engine_main',engine)
    assert service_commands.execute({'command':'assess','options':{'run_dir':str(source)}},target) == 0
    assert (source/'note.txt').read_text() == 'original'
    assert (target/'note.txt').read_text() == 'new assessment'


def test_active_source_tool_runs_in_owned_process_and_records_reads(api):
    server, client, root = api
    source = root/'active-source';(source/'passages').mkdir(parents=True)
    paragraph = {'id':'W2-p1','reference_id':'W2','page':1,'start':0,'end':18,'text':'Synthetic excerpt.'}
    (source/'passages/W2.jsonl').write_text(json.dumps(paragraph)+'\n')
    registry = server.RequestHandlerClass.supervisor.registry
    runs.register_start('active-source','fixture','',bundle=str(source),path=registry)
    response = client.post('/api/runs/active-source/tools/source',json={'reference_id':'W2'})
    assert response.status_code == 200, response.text
    assert response.json() == [paragraph]
    assert json.loads((source/'source-reads.jsonl').read_text())['passage_ids'] == ['W2-p1']
    receipts = list((root/'run-tools/active-source').glob('*/ownership.json'))
    assert len(receipts) == 1 and json.loads(receipts[0].read_text())['pid'] > 0
