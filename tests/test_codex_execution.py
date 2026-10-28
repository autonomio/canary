"""Exercise actual stdio/HTTP routing and failure isolation without model calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

import httpx
import jsonschema
import pytest

from canary import api_contract, codex_auth, codex_execution, coverage_runtime as engine, dashboard, runs
from test_coverage_evidence_ui import run_dashboard


FAKE = r'''#!/usr/bin/env python3
import json,os,pathlib,sqlite3,sys,threading,time
home=pathlib.Path(os.environ['CODEX_HOME']);home.mkdir(exist_ok=True)
if '--version' in sys.argv:
 print('codex-fixture 0.162.0');raise SystemExit()
lock=threading.RLock();threads={};count=0

def send(message):
 with lock: print(json.dumps(message),flush=True)

def log(method,params):
 with lock:
  with (home/'calls.jsonl').open('a') as f:f.write(json.dumps({'method':method,'params':params})+'\n')

def persist(tid):
 t=threads[tid];rollout=home/(tid+'.jsonl')
 rollout.write_text(json.dumps({'type':'turn_context','payload':{'turn_id':'turn-'+tid,'model':t['model'],'effort':t['effort']}})+'\n'+json.dumps({'type':'token_usage_record','payload':{'thread_id':tid,'turn_id':'turn-'+tid,'response_id':'response-'+tid,'usage':{'input_tokens':100,'cached_input_tokens':80,'cache_write_input_tokens':0,'output_tokens':20,'reasoning_output_tokens':10,'total_tokens':120}}})+'\n')
 with sqlite3.connect(home/'state_1.sqlite') as db:
  db.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,rollout_path TEXT,model TEXT,reasoning_effort TEXT)')
  db.execute('CREATE TABLE IF NOT EXISTS thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT)')
  db.execute('INSERT OR REPLACE INTO threads VALUES(?,?,?,?)',(tid,str(rollout),t['model'],t['effort']))

def turn(tid):
 t=threads[tid];cwd=pathlib.Path(t['cwd']);(cwd/'synthetic-committed.txt').write_text('Synthetic fixture only.')
 while (cwd/'hold').exists() and not t['cancelled']: time.sleep(.01)
 if not t['cancelled']:
  send({'method':'item/completed','params':{'threadId':tid,'item':{'type':'agentMessage','text':json.dumps({'marker':cwd.name,'value':17})}}})
 send({'method':'turn/completed','params':{'threadId':tid,'turn':{'id':'turn-'+tid,'status':'interrupted' if t['cancelled'] else 'completed'}}})

if sys.argv[1]=='exec':
 assert '--ignore-user-config' in sys.argv
 cwd=pathlib.Path.cwd();tid='exec-'+cwd.name;threads[tid]={'cwd':str(cwd),'model':sys.argv[sys.argv.index('-m')+1],'effort':'xhigh','cancelled':False}
 log('exec',{'argv':sys.argv,'cwd':str(cwd),'api_url':os.environ.get('CANARY_API_URL')})
 print(json.dumps({'type':'thread.started','thread_id':tid}),flush=True)
 pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'marker':cwd.name,'value':17}));persist(tid)
 print(json.dumps({'type':'turn.completed','usage':{}}),flush=True);raise SystemExit()
for line in sys.stdin:
 m=json.loads(line);method=m.get('method');params=m.get('params',{});result={}
 if method is None:continue
 log(method,params)
 if method=='config/read':
  user={'cli_auth_credentials_store':(home/'store').read_text() if (home/'store').exists() else 'file'}
  if (home/'personal').exists():user['developer_instructions']='PERSONAL-SENTINEL'
  result={'config':{'cli_auth_credentials_store':user.get('cli_auth_credentials_store','file')},'layers':[{'name':{'type':'user'},'config':user}]}
 elif method=='account/read':
  if (home/'reject-account').exists():
   send({'id':m['id'],'error':{'code':-32600,'message':'PRIVATE-TOKEN'}});continue
  result={'account':{'type':'chatgpt'}}
 elif method=='thread/start':
  count+=1;tid='thread-'+str(count);c=params['config'];threads[tid]={'cwd':params['cwd'],'model':params['model'],'effort':c['model_reasoning_effort'],'cancelled':False}
  result={'thread':{'id':tid},'model':params['model'],'reasoningEffort':c['model_reasoning_effort'],'cwd':params['cwd']}
 elif method=='turn/start':
  tid=params['threadId'];result={'turn':{'id':'turn-'+tid}}
  send({'id':m['id'],'result':result});threading.Thread(target=turn,args=(tid,),daemon=True).start();continue
 elif method=='turn/interrupt':threads[params['threadId']]['cancelled']=True
 elif method=='thread/archive':
  if (home/'fail-archive').exists():
   send({'id':m['id'],'error':{'code':-32600,'message':'PRIVATE-TOKEN'}});continue
  persist(params['threadId'])
 if 'id' in m:send({'id':m['id'],'result':result})
'''


def eventually(check, timeout=8):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if value:=check(): return value
        time.sleep(.01)
    raise AssertionError('Expected runtime transition did not complete')


@pytest.fixture
def native(tmp_path,monkeypatch):
    monkeypatch.delenv('CANARY_API_URL',raising=False)
    monkeypatch.delenv('CANARY_CODEX_CREDENTIALS_STORE',raising=False)
    home=tmp_path/'native';home.mkdir()
    fake=tmp_path/'codex';fake.write_text(FAKE);fake.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('CODEX_HOME',str(home))
    auth=codex_auth.CodexAuth(str(home));pool=codex_execution.CodexExecution(auth)
    yield pool,home,tmp_path
    pool.close()


def execute(pool,root,key,*,role='research',hold=False):
    workspace=root/key;workspace.mkdir(exist_ok=True)
    if hold:(workspace/'hold').touch()
    model,effort=('gpt-6.1-sol','xhigh') if role=='research' else ('gpt-6-astra','ultra')
    return engine.scientific_session(workspace,role,model,effort,'Synthetic fixture request',
        {'type':'object'},executable=str(root/'codex'),execution=pool,run_key=key,
        execution_environment={'RUN_KEY':key,'CANARY_API_URL':'http://127.0.0.1:9999'})


def calls(home):
    return [json.loads(line) for line in (home/'calls.jsonl').read_text().splitlines()]


def test_concurrent_sessions_use_one_process_with_independent_models_contexts_and_receipts(native):
    pool,home,root=native
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures=[workers.submit(execute,pool,root,'case-'+str(i),role='audit' if i%2 else 'research',hold=True) for i in range(4)]
        eventually(lambda:sum((root/('case-'+str(i))/'synthetic-committed.txt').exists() for i in range(4))==4)
        process=pool.auth.proc
        assert pool.status()['active_shared_sessions']==4 and process.poll() is None
        # Account replies use the same pipe while every scientific turn is busy.
        assert pool.auth.status()['authenticated']
        for i in range(4):(root/('case-'+str(i))/'hold').unlink()
        results=[f.result(timeout=10) for f in futures]
    assert len({receipt['thread_id'] for _,receipt in results})==4
    for i,(result,receipt) in enumerate(results):
        assert result=={'marker':'case-'+str(i),'value':17}
        assert receipt['backend']==codex_execution.SHARED
        assert receipt['cleanup']=={'archived':True}
        assert receipt['usage']['totals']['input_tokens']==100
        assert receipt['usage']['no_subagents_verified']
        jsonschema.validate(receipt,api_contract.schemas()['ExecutionReceipt'])
    assert pool.auth.proc is process and process.poll() is None
    assert pool.status()['active_shared_sessions']==0
    assert [c for c in calls(home) if c['method']=='exec']==[]
    starts=[c['params'] for c in calls(home) if c['method']=='thread/start']
    assert len(starts)==4 and all(s['config']['agents.enabled'] is False for s in starts)
    assert {s['config']['shell_environment_policy.set']['RUN_KEY'] for s in starts}=={'case-'+str(i) for i in range(4)}
    assert all(s['approvalsReviewer']=='auto_review' for s in starts)


def test_one_cancellation_leaves_other_turn_and_backend_alive(native):
    pool,home,root=native
    with ThreadPoolExecutor(max_workers=2) as workers:
        first=workers.submit(execute,pool,root,'cancelled',hold=True)
        other=workers.submit(execute,pool,root,'unrelated',hold=True)
        eventually(lambda:(root/'cancelled/synthetic-committed.txt').exists() and (root/'unrelated/synthetic-committed.txt').exists())
        pool.cancel('cancelled')
        with pytest.raises(RuntimeError,match='failed to produce'):first.result(timeout=10)
        assert not other.done() and pool.auth.proc.poll() is None
        (root/'unrelated/hold').unlink()
        assert other.result(timeout=10)[0]['marker']=='unrelated'
    receipt=json.loads((root/'cancelled/research-receipt.json').read_text())
    assert receipt['exit_code']==130 and receipt['cleanup']['archived']
    assert (root/'cancelled/synthetic-committed.txt').exists()


def test_backend_death_wakes_every_session_without_restarting_research(native):
    pool,home,root=native
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures=[workers.submit(execute,pool,root,key,hold=True) for key in ('lost-a','lost-b')]
        eventually(lambda:all((root/key/'synthetic-committed.txt').exists() for key in ('lost-a','lost-b')))
        process=pool.auth.proc;os.killpg(process.pid,signal.SIGKILL)
        for future in futures:
            with pytest.raises(RuntimeError,match='failed to produce'):future.result(timeout=10)
    assert pool.auth.proc is process and process.poll() is not None
    assert pool.status()['active_shared_sessions']==0
    assert len([c for c in calls(home) if c['method']=='thread/start'])==2
    assert all(json.loads((root/key/'research-receipt.json').read_text())['exit_code']!=0 for key in ('lost-a','lost-b'))


def test_personal_config_uses_explicit_proven_cli_without_exposing_or_modifying_it(native):
    pool,home,root=native
    (home/'personal').write_text('PERSONAL-SENTINEL')
    (home/'store').write_text('keyring')
    result,receipt=execute(pool,root,'fallback')
    assert result['marker']=='fallback' and receipt['backend']==codex_execution.LEGACY
    assert 'ignore-user-config' in receipt['fallback_reason']
    assert 'PERSONAL-SENTINEL' not in json.dumps(receipt)
    assert (home/'personal').read_text()=='PERSONAL-SENTINEL'
    launches=[c for c in calls(home) if c['method']=='exec']
    assert len(launches)==1 and 'cli_auth_credentials_store="keyring"' in launches[0]['params']['argv']
    assert launches[0]['params']['api_url']=='http://127.0.0.1:9999'


def test_native_rejection_and_cleanup_failure_do_not_destroy_findings_or_other_sessions(native):
    pool,home,root=native
    (home/'fail-archive').touch()
    result,receipt=execute(pool,root,'kept')
    assert result['marker']=='kept' and receipt['cleanup']['archived'] is False
    assert 'PRIVATE-TOKEN' not in json.dumps(receipt)
    (home/'reject-account').touch();process=pool.auth.proc
    assert pool.auth.status()['state']=='unavailable'
    assert pool.auth.proc is process and process.poll() is None
    (home/'fail-archive').unlink();(home/'reject-account').unlink()
    assert execute(pool,root,'next')[1]['cleanup']['archived'] is True


def test_audit_timeout_preserves_intermediate_audit_and_releases_only_its_thread(native):
    pool,home,root=native
    workspace=root/'audit';workspace.mkdir();(workspace/'hold').touch()
    engine.write(workspace/'audit.json',{'claim_checks':[],'limitations':['intermediate fixture']})
    result,receipt=engine.scientific_session(workspace,'audit','gpt-6-astra','ultra','Fixture',{},
        executable=str(root/'codex'),execution=pool,run_key='audit',audit_timeout=.15)
    assert receipt['timed_out'] and receipt['exit_code']==130 and receipt['cleanup']['archived']
    assert result['audit_complete'] is False and 'intermediate fixture' in result['limitations']
    assert pool.auth.proc.poll() is None


def test_http_execution_owns_parameters_and_frontend_discloses_backend(native,monkeypatch):
    pool,home,root=native
    server=dashboard.make_server(0,registry=root/'registry.json',root=root)
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth.close();supervisor.auth=pool.auth;supervisor.execution=pool
    workspace=root/'http';workspace.mkdir()
    engine.write(workspace/'run.json',{'question':'Synthetic HTTP fixture?','status':'researching','minimum_papers':50})
    runs.register_start('http','Fixture','',kind='question',bundle=str(workspace),status='running',path=supervisor.registry)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=20,trust_env=False) as client:
            assert client.post('/api/runs/missing/execution',json={'role':'research'}).status_code==404
            for body in ({},{'role':'unknown'},{'role':'research','model':'other'},{'role':'research','workspace':'/tmp'}):
                assert client.post('/api/runs/http/execution',json=body).status_code==400
            response=client.post('/api/runs/http/execution',json={'role':'research'})
            assert response.status_code==200,response.text
            jsonschema.validate(response.json(),api_contract.schemas()['ExecutionReceipt'])
            assert response.json()['model']=='gpt-6.1-sol'
            assert client.post('/api/runs/http/execution',json={'role':'research'}).status_code==400
            status=client.get('/api/status').json();assert status['execution']['backend']==codex_execution.SHARED
            assert client.post('/api/runs/http/execution',json={'role':'audit'}).status_code==400
        request=next(c for c in calls(home) if c['method']=='turn/start')
        assert 'Synthetic HTTP fixture?' in request['params']['input'][0]['text']
        assert request['params']['outputSchema']==engine.protocol.investigation_schema()
        run_dashboard(root,{'run':{'selection_method':'coverage-guided'},'profile':{'roles':{'research':response.json()}}},r'''
const html=coverageTokenHtml(graph);
assert.match(html,/Shared Codex server · separate conversation/);
assert.match(html,/thread-1/);
''')
    finally:
        server.shutdown();server.server_close();thread.join(5)


def test_duplicate_role_does_not_interrupt_the_original_and_shutdown_seals_receipts(native):
    pool,home,root=native
    with ThreadPoolExecutor(max_workers=2) as workers:
        original=workers.submit(execute,pool,root,'owned',hold=True)
        eventually(lambda:(root/'owned/synthetic-committed.txt').exists())
        before=(root/'owned/research-events.jsonl').read_bytes()
        with pytest.raises(ValueError,match='already active'):
            execute(pool,root,'owned')
        assert (root/'owned/research-events.jsonl').read_bytes()==before
        assert not original.done() and pool.status()['active_shared_sessions']==1
        pool.close()
        with pytest.raises(RuntimeError,match='failed to produce'):
            original.result(timeout=10)
    assert json.loads((root/'owned/research-receipt.json').read_text())['exit_code']!=0
    assert pool.auth.proc is None
    with pytest.raises(RuntimeError,match='stopping'):
        execute(pool,root,'after-close')


def test_cancel_before_native_initialization_prevents_late_model_start(native):
    pool,home,root=native
    assert pool.cancel('stopped-before-start') is None
    with pytest.raises(engine.RunCancelled,match='stopped before'):
        execute(pool,root,'stopped-before-start')
    assert not [c for c in calls(home) if c['method'] in ('thread/start','turn/start','exec')]
    assert not (root/'stopped-before-start/research-prompt.txt').exists()
