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


def test_tool_worker_uses_its_own_service_capacity_directory(api, monkeypatch):
    server, client, root = api
    workspace = root / 'active'; workspace.mkdir()
    (workspace / 'run.json').write_text(json.dumps({'status': 'researching'}))
    registry = server.RequestHandlerClass.supervisor.registry
    runs.register_start('active', 'active', '', bundle=str(workspace), status='researching', path=registry)
    monkeypatch.setenv('CANARY_DATA_DIR', str(root / 'unrelated-service'))
    original, seen = dashboard.subprocess.Popen, []
    def launch(*args, **kwargs):
        if 'env' in kwargs: seen.append(kwargs['env']['CANARY_DATA_DIR'])
        return original(*args, **kwargs)
    monkeypatch.setattr(dashboard.subprocess, 'Popen', launch)
    response = client.post('/api/runs/active/tools/discover', json={'plan': {}})
    assert response.status_code == 400  # Invalid plan; no provider call.
    assert seen == [str(root.resolve())]


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


@pytest.mark.parametrize('match,process_count', [(None,0), ('Synthetic|excerpt',0), ('Synthetic.*',1)])
def test_active_source_tool_preserves_results_and_read_receipts(api, match, process_count):
    server, client, root = api
    source = root/'active-source';(source/'passages').mkdir(parents=True)
    paragraph = {'id':'W2-p1','reference_id':'W2','page':1,'start':0,'end':18,'text':'Synthetic excerpt.'}
    (source/'passages/W2.jsonl').write_text(json.dumps(paragraph)+'\n')
    registry = server.RequestHandlerClass.supervisor.registry
    runs.register_start('active-source','fixture','',bundle=str(source),path=registry)
    response = client.post('/api/runs/active-source/tools/source',json={'reference_id':'W2','match':match})
    assert response.status_code == 200, response.text
    assert response.json() == [paragraph]
    assert json.loads((source/'source-reads.jsonl').read_text())['passage_ids'] == ['W2-p1']
    receipts = list((root/'run-tools/active-source').glob('*/ownership.json'))
    assert len(receipts) == process_count
    if receipts: assert json.loads(receipts[0].read_text())['pid'] > 0


@pytest.mark.parametrize('match', [None, 'Synthetic|excerpt', 'Synthetic.*'])
def test_unacquired_source_returns_the_same_structured_error_on_both_paths(api, match):
    server, client, root = api
    source = root/'missing-source'; source.mkdir()
    (source/'run.json').write_text(json.dumps({'status':'researching'}))
    runs.register_start('missing-source','fixture','',bundle=str(source),status='researching',
                        path=server.RequestHandlerClass.supervisor.registry)
    response = client.post('/api/runs/missing-source/tools/source', json={'reference_id':'W999','match':match})
    assert response.status_code == 400
    assert response.headers['content-type'].startswith('application/json')
    assert response.json() == {'error':str(FileNotFoundError(2, 'No such file or directory', str(source/'passages/W999.jsonl')))[:300]}
    assert not (source/'source-reads.jsonl').exists()
    assert client.post('/api/runs/missing-source/tools/catalog', json={}).json() == []


def test_inline_source_read_finishes_before_the_api_closes_writers(api, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    server, client, root = api
    source = root/'reader'; (source/'passages').mkdir(parents=True)
    (source/'run.json').write_text(json.dumps({'status':'researching'}))
    passage = {'id':'W1-p1','reference_id':'W1','page':1,'start':0,'end':4,'text':'Text'}
    (source/'passages/W1.jsonl').write_text(json.dumps(passage)+'\n')
    supervisor = server.RequestHandlerClass.supervisor
    runs.register_start('reader','reader','',bundle=str(source),status='researching', path=supervisor.registry)
    original, entered, release = research_tools.engine.source, threading.Event(), threading.Event()
    finishing, attempted, acquired = threading.local(), threading.Event(), []
    original_lock, original_finish = supervisor.lock, supervisor.finish_execution
    class ObservedLock:
        def __enter__(self):
            immediate = original_lock.acquire(blocking=False)
            if getattr(finishing, 'active', False):
                acquired.append(immediate)
                attempted.set()  # The finish handler has actually attempted admission.
            if not immediate: original_lock.acquire()
            return self
        def __exit__(self, *args):
            original_lock.release()
    def finish(*args, **kwargs):
        finishing.active = True
        try: return original_finish(*args, **kwargs)
        finally: finishing.active = False
    monkeypatch.setattr(supervisor, 'lock', ObservedLock())
    monkeypatch.setattr(supervisor, 'finish_execution', finish)
    def read(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(research_tools.engine, 'source', read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(client.post, '/api/runs/reader/tools/source', json={'reference_id':'W1'})
        try:
            assert entered.wait(5)
            closing = pool.submit(client.post, '/api/runs/reader/execution/finish', json={'cancelled':False})
            assert attempted.wait(5)
            assert acquired == [False]  # Finish is blocked by this actual reader.
            assert not runs.get('reader', supervisor.registry).get('execution_closed')
            assert not (source/'source-reads.jsonl').exists()
        finally:
            release.set()
        assert reading.result(5).json() == [passage]
        assert closing.result(5).status_code == 200
    before = (source/'source-reads.jsonl').read_bytes()
    assert client.post('/api/runs/reader/tools/source', json={'reference_id':'W1'}).status_code == 200
    assert (source/'source-reads.jsonl').read_bytes() == before
