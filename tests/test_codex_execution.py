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

from canary import api_contract, codex_auth, codex_execution, coverage_export, coverage_runtime as engine, dashboard, runs
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
 if (home/'capacity').exists():
  # codex-cli prints only the message; the code stays in the session's terminal task_complete.
  capacity='Selected model is at capacity. Please try a different model.';sys.stdin.read()
  (cwd/'synthetic-committed.txt').write_text('Synthetic fixture only.')
  print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'command_execution','command':'canary catalog','exit_code':0,'status':'completed'}}),flush=True)
  persist(tid)
  with (home/(tid+'.jsonl')).open('a') as f:f.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete','last_agent_message':None,'error':{'message':capacity,'codex_error_info':'server_overloaded'}}})+'\n')
  print(json.dumps({'type':'error','message':capacity}),flush=True)
  print(json.dumps({'type':'turn.failed','error':{'message':capacity}}),flush=True);raise SystemExit(1)
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
    supervisor.auth.close();supervisor.auth=pool.auth;supervisor.execution=pool;pool.can_start=supervisor._can_execute
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


def test_native_capacity_failure_reaches_run_research_and_frontend(native):
    # Issue #148: terminal server_overloaded after research activity, codex exec exit 1.
    pool,home,root=native
    (home/'personal').write_text('PERSONAL-SENTINEL');(home/'capacity').touch()
    capacity='Selected model is at capacity. Please try a different model.'
    expected={'code':'server_overloaded','message':capacity,'category':'transient_provider_capacity','same_run_retryable':False}
    server=dashboard.make_server(0,registry=root/'registry.json',root=root)
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth.close();supervisor.auth=pool.auth;supervisor.execution=pool;pool.can_start=supervisor._can_execute
    workspace=root/'capacity';workspace.mkdir()
    engine.write(workspace/'run.json',{'kind':'question','question':'Synthetic capacity fixture?','status':'researching','minimum_papers':50})
    runs.register_start('capacity','Fixture','',kind='question',bundle=str(workspace),status='running',path=supervisor.registry)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=20,trust_env=False) as client:
            failed=client.post('/api/runs/capacity/execution',json={'role':'research'})
            assert failed.status_code==400 and 'native Codex error server_overloaded: '+capacity in failed.json()['error']
            receipt=json.loads((workspace/'research-receipt.json').read_text())
            assert receipt['backend']==codex_execution.LEGACY and receipt['exit_code']==1 and receipt['timed_out'] is False
            assert receipt['native_error']==expected and (workspace/'synthetic-committed.txt').exists()
            jsonschema.validate(receipt,api_contract.schemas()['ExecutionReceipt'])
            # The worker records its failure and finalizes as run_compiled does.
            engine.write(workspace/'run.json',dict(engine.read(workspace/'run.json'),status='failed',failure='RuntimeError: '+failed.json()['error']))
            coverage_export.export_bundle(workspace)
            runs.register_finish('capacity','failed',str(workspace),path=supervisor.registry)
            row=client.get('/api/runs/capacity').json()
            research=client.get('/api/runs/capacity/research').json()
            contract=client.get('/openapi.json').json()
    finally:
        server.shutdown();server.server_close();thread.join(5)
    assert row['status']=='failed' and row['native_error']==expected
    role=research['research']['profile']['roles']['research']
    assert role['exit_code']==1 and role['timed_out'] is False and role['native_error']==expected
    assert 'server_overloaded: '+capacity in research['research']['run']['failure']
    for path,value in (('/api/runs/{key}',row),('/api/runs/{key}/research',research)):
        schema=contract['paths'][path]['get']['responses']['200']['content']['application/json']['schema']
        jsonschema.Draft202012Validator(dict(schema,components=contract['components'])).validate(value)
    # One bounded attempt with the selected model; no silent retry or model switch.
    launches=[c['params']['argv'] for c in calls(home) if c['method']=='exec']
    assert len(launches)==1 and launches[0][launches[0].index('-m')+1]=='gpt-6.1-sol'
    run_dashboard(root,{'row':row,'research':research['research']},r'''
const html=answerHtml(graph.row,graph.research), issue=diagnoseRun(graph.row,{});
assert.match(html,/Model capacity unavailable · research · <code>server_overloaded<\/code>/);
assert.match(html,/Selected model is at capacity\. Please try a different model\./);
assert.match(html,/not a scientific refusal or a research error/);
assert.match(html,/does not retry a scientific role within this run/);
assert.equal(issue.title,'Model capacity was unavailable');
assert.equal(issue.text,'server_overloaded: Selected model is at capacity. Please try a different model.');
assert.match(issue.next,/new run with the same saved model and settings/);
''')


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


def test_delayed_duplicate_is_rejected_after_first_role_closes_and_receipt_is_removed(native,monkeypatch):
    # Hold the raw active phase stable while scheduling the delayed request;
    # opportunistic UI exports are independent of durable role admission.
    monkeypatch.setattr(engine,'refresh_committed_progress',lambda *a,**kw:None)
    pool,home,root=native
    server=dashboard.make_server(0,registry=root/'registry.json',root=root)
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth=pool.auth;supervisor.execution=pool;pool.can_start=supervisor._can_execute
    workspace=root/'duplicate';workspace.mkdir();(workspace/'hold').touch()
    engine.write(workspace/'run.json',{'question':'Fixture?','status':'researching'})
    runs.register_start('duplicate','Fixture','',kind='question',bundle=str(workspace),status='running',path=supervisor.registry)
    open_session=pool.open;count=0;entered=threading.Event();release=threading.Event()
    def late_open(*args,**kwargs):
        nonlocal count
        count+=1
        if count>1:
            entered.set();assert release.wait(8)
        return open_session(*args,**kwargs)
    monkeypatch.setattr(pool,'open',late_open)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def request():
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=15,trust_env=False) as client:
            return client.post('/api/runs/duplicate/execution',json={'role':'research'})
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first=workers.submit(request)
            eventually(lambda:(workspace/'synthetic-committed.txt').exists())
            duplicate=workers.submit(request)
            eventually(lambda:duplicate.done() or entered.is_set())
            (workspace/'hold').unlink()
            assert first.result(timeout=10).status_code==200
            original=(workspace/'research-receipt.json').read_bytes()
            release.set()
            assert duplicate.result(timeout=10).status_code==400
            assert (workspace/'research-receipt.json').read_bytes()==original
        # The persisted admission claim survives missing artifacts and a new
        # supervisor; a receipt-file check alone cannot establish this property.
        (workspace/'research-receipt.json').unlink()
        restarted=dashboard.Supervisor(registry=supervisor.registry,root=root)
        with pytest.raises(ValueError,match='already started'):
            restarted.execute_scientific('duplicate',{'role':'research'})
        assert len([c for c in calls(home) if c['method']=='thread/start'])==1
    finally:
        release.set();server.shutdown();server.server_close();thread.join(5)


@pytest.mark.parametrize('reaped',[False,True])
def test_fallback_cancellation_uses_birth_identity_and_never_captures_a_reused_pid(native,monkeypatch,reaped):
    from types import SimpleNamespace
    pool,_,root=native
    session=codex_execution.Session(pool,'birth','research',root,'gpt-6.1-sol','xhigh',{},root/'out',None,[],False,None,'file',{})
    session.process=SimpleNamespace(pid=12345,poll=lambda:0 if reaped else None)
    session.process_receipt={'pid':12345,'identity':'original-child-birth'}
    stopped=[]
    monkeypatch.setattr(codex_execution,'identity',lambda _:pytest.fail('Captured a replacement PID identity'))
    monkeypatch.setattr(codex_execution,'stop_tree',lambda record,**kw:stopped.append(record))
    session.interrupt()
    assert stopped==([] if reaped else [session.process_receipt])


def test_finish_closes_admission_before_a_late_native_start(native):
    pool,home,root=native
    server=dashboard.make_server(0,registry=root/'registry.json',root=root)
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth=pool.auth;supervisor.execution=pool;pool.can_start=supervisor._can_execute
    workspace=root/'closing';workspace.mkdir();engine.write(workspace/'run.json',{'status':'researching'})
    runs.register_start('closing','Fixture','',kind='question',bundle=str(workspace),status='running',path=supervisor.registry)
    try:
        assert supervisor.finish_execution('closing',{'cancelled':True})=={'key':'closing','cancelled':True}
        with pytest.raises(engine.RunCancelled,match='stopped before'):
            execute(pool,root,'closing')
        assert not [c for c in calls(home) if c['method']=='thread/start']
    finally:
        server.server_close()
