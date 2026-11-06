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

from canary import api_contract, cli, codex_auth, codex_execution, coverage_export, coverage_runtime as engine, dashboard, runs
from test_coverage_evidence_ui import run_dashboard


FAKE = r'''#!/usr/bin/env python3
import json,os,pathlib,sqlite3,subprocess,sys,threading,time
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
 rows=[]
 for n in range(1,t.get('turns',1)+1):
  turn_id='turn-'+tid+'-'+str(n)
  rows.extend([{'type':'turn_context','payload':{'turn_id':turn_id,'model':t['model'],'effort':t['effort']}}, {'type':'token_usage_record','payload':{'thread_id':tid,'turn_id':turn_id,'response_id':'response-'+tid+'-'+str(n),'usage':{'input_tokens':100,'cached_input_tokens':80,'cache_write_input_tokens':0,'output_tokens':20,'reasoning_output_tokens':10,'total_tokens':120}}}])
 if t.get('error'):rows.append({'type':'event_msg','payload':{'type':'task_complete','last_agent_message':None,'error':t['error']}})
 rollout.write_text(''.join(json.dumps(r)+'\n' for r in rows))
 with sqlite3.connect(home/'state_1.sqlite') as db:
  db.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,rollout_path TEXT,model TEXT,reasoning_effort TEXT)')
  db.execute('CREATE TABLE IF NOT EXISTS thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT)')
  db.execute('INSERT OR REPLACE INTO threads VALUES(?,?,?,?)',(tid,str(rollout),t['model'],t['effort']))

CAPACITY='Selected model is at capacity. Please try a different model.'
def overloaded(tid):
 # Consumes one scripted capacity failure. codex-cli prints only the message; the code stays in the session's task_complete.
 with lock:
  counter=home/'capacity'
  if not counter.exists() or not int(counter.read_text() or 0):return False
  counter.write_text(str(int(counter.read_text())-1))
 threads[tid]['error']={'message':CAPACITY,'codex_error_info':'server_overloaded'}
 (pathlib.Path(threads[tid]['cwd'])/'committed-before-capacity.txt').write_text('Synthetic review committed before capacity.')
 return True

def turn(tid):
 t=threads[tid];cwd=pathlib.Path(t['cwd']);(cwd/'synthetic-committed.txt').write_text('Synthetic fixture only.')
 while (cwd/'hold').exists() and not t['cancelled']: time.sleep(.01)
 if not t['cancelled'] and overloaded(tid):
  send({'method':'item/completed','params':{'threadId':tid,'item':{'type':'commandExecution','command':'canary catalog','status':'completed'}}})
  send({'method':'turn/completed','params':{'threadId':tid,'turn':{'id':'turn-'+tid,'status':'failed'}}});return
 if not t['cancelled']:
  t['error']=None
  # Shell commands the session runs, with the environment Canary set for it.
  for command in t.get('scripted',{}).get('commands',[]):
   done=subprocess.run(command,shell=True,cwd=cwd,env=dict(os.environ,**t.get('env',{})),capture_output=True,text=True,timeout=60)
   with lock:
    with (home/'commands.jsonl').open('a') as f:f.write(json.dumps({'command':command,'exit_code':done.returncode,'stderr':done.stderr})+'\n')
  for name,value in t.get('scripted',{}).get('files',{}).items():
   path=cwd/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)
  send({'method':'item/completed','params':{'threadId':tid,'item':{'type':'agentMessage','text':json.dumps(t.get('scripted',{}).get('response',{'marker':cwd.name,'value':17}))}}})
 send({'method':'turn/completed','params':{'threadId':tid,'turn':{'id':'turn-'+tid,'status':'interrupted' if t['cancelled'] else 'completed'}}})

if sys.argv[1]=='exec':
 assert '--ignore-user-config' in sys.argv
 cwd=pathlib.Path.cwd();tid=sys.argv[-2] if 'resume' in sys.argv else ('exec-audit-'+str(os.getpid()) if pathlib.Path(sys.argv[sys.argv.index('-o')+1]).name=='audit-response.json' else 'exec-'+cwd.name);threads[tid]={'cwd':str(cwd),'model':sys.argv[sys.argv.index('-m')+1],'effort':'xhigh','cancelled':False}
 previous=home/(tid+'.jsonl');threads[tid]['turns']=sum(json.loads(line).get('type')=='token_usage_record' for line in previous.read_text().splitlines())+1 if previous.exists() else 1
 log('exec',{'argv':sys.argv,'cwd':str(cwd),'api_url':os.environ.get('CANARY_API_URL'),'prompt':sys.stdin.read()})
 print(json.dumps({'type':'thread.started','thread_id':tid}),flush=True)
 if overloaded(tid):
  print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'command_execution','command':'canary catalog','exit_code':0,'status':'completed'}}),flush=True)
  persist(tid)
  print(json.dumps({'type':'error','message':CAPACITY}),flush=True)
  print(json.dumps({'type':'turn.failed','error':{'message':CAPACITY}}),flush=True);raise SystemExit(1)
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
  count+=1;tid='thread-'+str(count);c=params['config'];threads[tid]={'cwd':params['cwd'],'model':params['model'],'effort':c['model_reasoning_effort'],'cancelled':False,'env':c.get('shell_environment_policy.set',{})}
  result={'thread':{'id':tid},'model':params['model'],'reasoningEffort':c['model_reasoning_effort'],'cwd':params['cwd']}
 elif method=='thread/resume':
  tid=params['threadId'];t=threads[tid];t['cancelled']=False;t['env']=params.get('config',{}).get('shell_environment_policy.set',{})
  result={'thread':{'id':tid},'model':t['model'],'reasoningEffort':t['effort'],'cwd':t['cwd']}
 elif method=='turn/start':
  tid=params['threadId'];threads[tid]['turns']=threads[tid].get('turns',0)+1
  fixture=home/'scripted.json'
  if fixture.exists():
   data=json.loads(fixture.read_text());props=params.get('outputSchema',{}).get('properties',{})
   role='correction' if 'responses' in props else 'audit' if 'claim_checks' in props else 'research'
   threads[tid]['scripted']=data[role].pop(0);fixture.write_text(json.dumps(data))
  result={'turn':{'id':'turn-'+tid}}
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


def test_audit_timeout_fails_closed_preserves_intermediate_audit_and_releases_only_its_thread(native):
    pool,home,root=native
    workspace=root/'audit';workspace.mkdir();(workspace/'hold').touch()
    engine.write(workspace/'audit.json',{'claim_checks':[],'limitations':['intermediate fixture']})
    with pytest.raises(RuntimeError,match='time budget; its intermediate audit is preserved and no verdict was accepted'):
        engine.scientific_session(workspace,'audit','gpt-6-astra','ultra','Fixture',{},
            executable=str(root/'codex'),execution=pool,run_key='audit',audit_timeout=.15)
    receipt=json.loads((workspace/'audit-receipt.json').read_text())
    assert receipt['timed_out'] and receipt['exit_code']==130 and receipt['cleanup']['archived']
    partial=json.loads((workspace/'audit.json').read_text())
    assert partial['audit_complete'] is False and 'intermediate fixture' in partial['limitations']
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


def test_native_capacity_failure_reaches_run_research_and_frontend(native,monkeypatch):
    # Issues #148 and #158: terminal server_overloaded after research activity, codex exec exit 1,
    # on every resumption until the pause bound (0.125 s, then 0.25 s) is exhausted.
    pool,home,root=native
    (home/'personal').write_text('PERSONAL-SENTINEL');(home/'capacity').write_text('99')
    monkeypatch.setattr(engine,'CAPACITY_PAUSE_SECONDS',.125);monkeypatch.setattr(engine,'CAPACITY_PAUSE_BOUND_SECONDS',.375)
    capacity='Selected model is at capacity. Please try a different model.'
    expected={'code':'server_overloaded','message':capacity,'category':'transient_provider_capacity','same_run_retryable':True}
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
            assert failed.status_code==400 and ('model capacity pause bound of 0.375 s exhausted after 2 resumptions; '
                'native Codex error server_overloaded: '+capacity) in failed.json()['error']
            receipt=json.loads((workspace/'research-receipt.json').read_text())
            assert receipt['backend']==codex_execution.LEGACY and receipt['exit_code']==1 and receipt['timed_out'] is False
            assert receipt['native_error']==expected and (workspace/'committed-before-capacity.txt').exists()
            assert [(p['seconds'],p['paused_seconds'],p['resumed']) for p in receipt['capacity_pauses']]==[(.125,.125,True),(.25,.375,True)]
            assert all(p['native_error']==expected and p['bound_seconds']==.375 and p['role']=='research' for p in receipt['capacity_pauses'])
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
    assert row['status']=='failed' and row['native_error']==expected and row['capacity_pause'] is None
    role=research['research']['profile']['roles']['research']
    assert role['exit_code']==1 and role['timed_out'] is False and role['native_error']==expected
    assert role['capacity_pauses']==receipt['capacity_pauses']
    assert 'server_overloaded: '+capacity in research['research']['run']['failure']
    for path,value in (('/api/runs/{key}',row),('/api/runs/{key}/research',research)):
        schema=contract['paths'][path]['get']['responses']['200']['content']['application/json']['schema']
        jsonschema.Draft202012Validator(dict(schema,components=contract['components'])).validate(value)
    # Each resumption continues the same native thread with the selected model; no model switch.
    launches=[c['params'] for c in calls(home) if c['method']=='exec']
    assert len(launches)==3 and all(l['argv'][l['argv'].index('-m')+1]=='gpt-6.1-sol' for l in launches)
    assert 'resume' not in launches[0]['argv'] and 'Synthetic capacity fixture?' in launches[0]['prompt']
    assert all('resume' in l['argv'] and l['argv'][-2]==receipt['thread_id'] and
               l['prompt']==engine.protocol.CAPACITY_RESUME_PROMPT for l in launches[1:])
    run_dashboard(root,{'row':row,'research':research['research']},r'''
const html=answerHtml(graph.row,graph.research), issue=diagnoseRun(graph.row,{});
assert.match(html,/Model capacity unavailable · research · <code>server_overloaded<\/code>/);
assert.match(html,/Selected model is at capacity\. Please try a different model\./);
assert.match(html,/not a scientific refusal or a research error/);
assert.match(html,/Recorded pauses: 2 \(0\.375 s\)/);
assert.match(html,/Model capacity pauses · research · 2 · 0\.375 s/);
assert.doesNotMatch(html,/does not retry a scientific role within this run/);
assert.equal(issue.title,'Model capacity was unavailable');
assert.equal(issue.text,'server_overloaded: Selected model is at capacity. Please try a different model.');
assert.match(issue.next,/paused and resumed the same native session within this run/);
assert.match(issue.next,/new run with the same saved model and settings/);
''')


CAPACITY_ERROR={'code':'server_overloaded','message':'Selected model is at capacity. Please try a different model.',
                'category':'transient_provider_capacity','same_run_retryable':True}


def capacity_server(pool,root,key):
    """The run API over the fixture pool, with one registered researching run."""
    server=dashboard.make_server(0,registry=root/'registry.json',root=root)
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth.close();supervisor.auth=pool.auth;supervisor.execution=pool;pool.can_start=supervisor._can_execute
    workspace=root/key;workspace.mkdir()
    engine.write(workspace/'run.json',{'kind':'question','question':'Synthetic capacity pause?','status':'researching','minimum_papers':50})
    runs.register_start(key,'Fixture','',kind='question',bundle=str(workspace),status='running',path=supervisor.registry)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    return server,thread,workspace


@pytest.mark.parametrize('backend',['exec','shared'])
def test_capacity_pause_resumes_the_same_session_and_keeps_earlier_work(native,monkeypatch,backend):
    # Issue #158: the terminal capacity event arrives after research activity; the run pauses, then resumes.
    pool,home,root=native
    if backend=='exec':(home/'personal').write_text('PERSONAL-SENTINEL')
    (home/'capacity').write_text('1');monkeypatch.setattr(engine,'CAPACITY_PAUSE_SECONDS',.125)
    server,thread,workspace=capacity_server(pool,root,'paused')
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=20,trust_env=False) as client:
            response=client.post('/api/runs/paused/execution',json={'role':'research'})
            finished=client.get('/api/runs/paused').json()
    finally:
        server.shutdown();server.server_close();thread.join(5)
    assert response.status_code==200,response.text
    receipt=response.json();jsonschema.validate(receipt,api_contract.schemas()['ExecutionReceipt'])
    assert receipt['exit_code']==0 and receipt['native_error'] is None and finished['capacity_pause'] is None
    [pause]=receipt['capacity_pauses']
    assert pause['role']=='research' and pause['native_error']==CAPACITY_ERROR and pause['resumed'] is True and pause['ended_at']
    assert (pause['seconds'],pause['paused_seconds'],pause['bound_seconds'])==(.125,.125,600)
    # Earlier work stays committed, both attempts' native responses are counted, and the resumed turn completed the role.
    assert (workspace/'committed-before-capacity.txt').read_text()=='Synthetic review committed before capacity.'
    assert receipt['usage']['response_count']==2 and receipt['model']=='gpt-6.1-sol'
    assert json.loads((workspace/'investigation.json').read_text())=={'marker':'paused','value':17}
    events=[json.loads(line)['event'] for line in (workspace/'runtime-progress.jsonl').read_text().splitlines()]
    assert events.index('research_capacity_pause')<events.index('research_capacity_resumed')<events.index('research_finished')
    recorded=calls(home)
    if backend=='exec':
        launches=[c['params'] for c in recorded if c['method']=='exec']
        assert len(launches)==2 and 'resume' in launches[1]['argv'] and launches[1]['argv'][-2]==receipt['thread_id']
        assert launches[1]['prompt']==engine.protocol.CAPACITY_RESUME_PROMPT
    else:
        assert [c['method'] for c in recorded].count('thread/start')==1
        assert [c['params']['threadId'] for c in recorded if c['method']=='thread/resume']==[receipt['thread_id']]
        turns=[c['params'] for c in recorded if c['method']=='turn/start']
        assert len(turns)==2 and turns[1]['input'][0]['text']==engine.protocol.CAPACITY_RESUME_PROMPT
        assert turns[1]['threadId']==receipt['thread_id'] and turns[1]['outputSchema']==turns[0]['outputSchema']
    run_dashboard(root,{'receipt':receipt},r'''
const done=capacityPauseHtml({capacity_pause:null},{profile:{roles:{research:graph.receipt}}});
assert.match(done,/Model capacity pauses · research · 1 · 0\.125 s/);
assert.match(done,/0\.125 s · resumed the same session · <code>server_overloaded<\/code>/);
assert.doesNotMatch(done,/Waiting for model capacity/);
''')


def test_capacity_pause_is_live_in_api_cli_and_dashboard_until_cancel_stops_it(native,monkeypatch,capsys):
    pool,home,root=native
    (home/'capacity').write_text('1');monkeypatch.setattr(engine,'CAPACITY_PAUSE_SECONDS',60)
    server,thread,workspace=capacity_server(pool,root,'waiting')
    base=f'http://127.0.0.1:{server.server_port}'
    def execute_research():
        with httpx.Client(base_url=base,timeout=30,trust_env=False) as client:
            return client.post('/api/runs/waiting/execution',json={'role':'research'})
    try:
        with httpx.Client(base_url=base,timeout=20,trust_env=False) as client, ThreadPoolExecutor(max_workers=1) as workers:
            pending=workers.submit(execute_research)
            row=eventually(lambda:(found:=client.get('/api/runs/waiting').json())['capacity_pause'] and found)
            assert cli.main(['runs','list','--port',str(server.server_port)])==0
            listed=capsys.readouterr().out
            contract=client.get('/openapi.json').json()
            cancelled=time.monotonic();pool.cancel('waiting')
            stopped=pending.result(timeout=10)
            after=client.get('/api/runs/waiting').json()
    finally:
        server.shutdown();server.server_close();thread.join(5)
    assert time.monotonic()-cancelled<5
    pause=row['capacity_pause']
    assert pause['role']=='research' and pause['native_error']==CAPACITY_ERROR and pause['ended_at'] is None and pause['resumed'] is None
    assert (pause['seconds'],pause['paused_seconds'],pause['bound_seconds'])==(60,60,600)
    assert 'waiting for model capacity; research resumes '+pause['resume_at'] in listed
    schema=contract['paths']['/api/runs/{key}']['get']['responses']['200']['content']['application/json']['schema']
    jsonschema.Draft202012Validator(dict(schema,components=contract['components'])).validate(row)
    # Cancellation interrupts the pause: the role stops with its receipt and never resumes.
    assert stopped.status_code==400 and 'failed to produce' in stopped.json()['error'] and after['capacity_pause'] is None
    receipt=json.loads((workspace/'research-receipt.json').read_text())
    [record]=receipt['capacity_pauses']
    assert record['resumed'] is False and record['ended_at'] and receipt['exit_code']==1
    assert [c['method'] for c in calls(home)].count('turn/start')==1 and pool.status()['active_shared_sessions']==0
    run_dashboard(root,{'row':row,'receipt':receipt},r'''
const [kind,label,text]=statusInfo(graph.row);
assert.equal(label,'Waiting for model capacity');
assert.match(text,/same research session resumes at \d\d:\d\d:\d\d UTC on the same model and account; committed work is kept/);
const live=capacityPauseHtml(graph.row,{});
assert.match(live,/Waiting for model capacity · research · resumes \d\d:\d\d:\d\d UTC/);
assert.match(live,/Selected model is at capacity\. Please try a different model\./);
assert.match(live,/This pause 60 s · 60 of 600 s pause bound used/);
const ended=capacityPauseHtml({capacity_pause:null},{profile:{roles:{research:graph.receipt}}});
assert.match(ended,/ended without resuming after \d+(\.\d+)? s of a 60 s pause/);
assert.doesNotMatch(ended,/· 60 s<\/summary>/);
''')


def test_capacity_resumption_is_recorded_only_once_the_resumed_turn_starts(native,monkeypatch):
    # A cancel lands just after the resumed session is registered: no turn starts, so nothing was resumed.
    pool,home,root=native
    (home/'capacity').write_text('1');monkeypatch.setattr(engine,'CAPACITY_PAUSE_SECONDS',.125)
    opened=pool.open
    def open_then_cancel(*args,**kwargs):
        session=opened(*args,**kwargs)
        if kwargs.get('replacing') is not None:pool.cancel(args[0])
        return session
    monkeypatch.setattr(pool,'open',open_then_cancel)
    with pytest.raises(RuntimeError,match='failed to produce'):execute(pool,root,'unresumed')
    receipt=json.loads((root/'unresumed/research-receipt.json').read_text())
    [pause]=receipt['capacity_pauses']
    assert pause['resumed'] is False and pause['ended_at'] and receipt['exit_code']==130
    events=[json.loads(line)['event'] for line in (root/'unresumed/runtime-progress.jsonl').read_text().splitlines()]
    assert 'research_capacity_pause' in events and 'research_capacity_resumed' not in events
    assert [c['method'] for c in calls(home)].count('turn/start')==1 and pool.status()['active_shared_sessions']==0


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


def test_api_audit_uses_sol_xhigh_without_a_deadline(native,monkeypatch):
    pool,home,root=native
    server=dashboard.make_server(0,registry=root/'registry.json',root=root)
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth.close();supervisor.auth=pool.auth;supervisor.execution=pool
    workspace=root/'unlimited';workspace.mkdir()
    engine.write(workspace/'run.json',{'question':'Synthetic audit?','status':'auditing','audit_budget_seconds':None})
    runs.register_start('unlimited','Fixture','',kind='question',bundle=str(workspace),status='running',path=supervisor.registry)
    captured=[]
    def scientific(*args,**kwargs):
        captured.append((args,kwargs))
        return {},{'model':args[2],'reasoning_effort':args[3],'timed_out':False}
    monkeypatch.setattr(engine,'scientific_session',scientific)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=10,trust_env=False) as client:
            response=client.post('/api/runs/unlimited/execution',json={'role':'audit'})
        assert response.status_code==200,response.text
        args,kwargs=captured[0]
        assert args[2:4]==('gpt-6.1-sol','xhigh')
        assert 'No audit time limit' in args[4]
        assert 'uncertainty_review' in args[4] and 'freshness policy' in args[4]
        assert args[5]['properties']['claim_checks']['items']['properties']['uncertainty_review']['required']
        assert len(captured) == 1
        assert kwargs['audit_timeout'] is None
    finally:
        server.shutdown();server.server_close();thread.join(5)


@pytest.mark.parametrize('role', ['research', 'audit'])
def test_http_execution_receives_recorded_research_state(native, role):
    pool,home,root=native
    server=dashboard.make_server(0,registry=root/'registry.json',root=root)
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth.close();supervisor.auth=pool.auth;supervisor.execution=pool;pool.can_start=supervisor._can_execute
    workspace=root/'continued';workspace.mkdir()
    engine.write(workspace/'research-state.json', {'references':[{'id':'W-old'}]})
    engine.write(workspace/'run.json', {'question':'Synthetic continuation?',
        'status':'researching' if role=='research' else 'auditing', 'minimum_papers':50,
        'research_state':{'continue_research':True,'sha256':'a'*64}})
    runs.register_start('continued','Synthetic continuation','',kind='question',bundle=str(workspace),status='running',path=supervisor.registry)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=20,trust_env=False) as client:
            response=client.post('/api/runs/continued/execution',json={'role':role})
        assert response.status_code==200,response.text
        request=next(c for c in calls(home) if c['method']=='turn/start')['params']
        prompt=request['input'][0]['text']
        assert 'research-state.json' in prompt
        assert 'gpt-6.1-sol'==response.json()['model'] and response.json()['reasoning_effort']=='xhigh'
        if role=='research':
            assert 'Continue research from this supplied state' in prompt
            assert 'No earlier experiment assets' not in prompt
        else:assert 'inherited findings and reviews were not counted as fresh work' in prompt
    finally:
        server.shutdown();server.server_close();thread.join(5)


@pytest.mark.parametrize('backend', ['shared', 'exec'])
def test_continuation_preserves_research_thread_and_counts_only_new_responses(native, backend):
    pool,home,root = native
    if backend == 'exec': (home/'personal').touch()
    workspace=root/'resume';workspace.mkdir()
    _, first=execute(pool,root,'resume')
    original=(workspace/'research-receipt.json').read_bytes()
    _, second=engine.scientific_session(workspace,'correction','gpt-6.1-sol','xhigh','Synthetic continuation',{},
        executable=str(root/'codex'),execution=pool,run_key='resume',round_index=1,
        resume_thread_id=first['thread_id'],expected_backend=first['backend'])
    assert first['thread_id'] == second['thread_id']
    assert second['usage']['totals']['input_tokens'] == 100
    assert second['thread_usage']['totals']['input_tokens'] == 200
    assert (workspace/'research-receipt.json').read_bytes() == original
    assert (workspace/'rounds/001/correction-receipt.json').is_file()
    _, audit=engine.scientific_session(workspace,'audit','gpt-6.1-sol','xhigh','Fresh synthetic audit',{},
        executable=str(root/'codex'),execution=pool,run_key='resume',round_index=1)
    assert audit['thread_id'] != first['thread_id']
    if backend == 'shared':
        methods=[c['method'] for c in calls(home)]
        assert methods.count('thread/start')==2 and methods.count('thread/resume')==1
    else:
        command=next(c['params']['argv'] for c in calls(home) if c['method']=='exec' and 'resume' in c['params']['argv'])
        assert command.index('--approve-for-me') < command.index('resume') < command.index('--output-schema')


def test_http_correction_round_is_leased_once_and_resumes_only_server_owned_thread(native):
    pool,home,root=native
    server=dashboard.make_server(0,registry=root/'registry.json',root=root)
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth.close();supervisor.auth=pool.auth;supervisor.execution=pool;pool.can_start=supervisor._can_execute
    workspace=root/'round-http';workspace.mkdir()
    manifest={'question':'Synthetic HTTP correction?','status':'researching','minimum_papers':1,'max_correction_rounds':1}
    engine.write(workspace/'run.json',manifest)
    runs.register_start('round-http','Fixture','',kind='question',bundle=str(workspace),status='running',path=supervisor.registry)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=20,trust_env=False) as client:
            first=client.post('/api/runs/round-http/execution',json={'role':'research'}).json()
            manifest.update(status='correcting',correction_round=1)
            engine.write(workspace/'run.json',manifest)
            engine.write(workspace/'rounds/001/correction-input.json',{'claim_ids':['C1']})
            for body in ({'role':'correction'}, {'role':'correction','round':2}, {'role':'correction','round':True},
                         {'role':'correction','round':1,'thread_id':'another-thread'}):
                assert client.post('/api/runs/round-http/execution',json=body).status_code==400
            second=client.post('/api/runs/round-http/execution',json={'role':'correction','round':1})
            assert second.status_code==200,second.text
            jsonschema.validate(second.json(),api_contract.schemas()['ExecutionReceipt'])
            assert second.json()['thread_id']==first['thread_id']
            assert client.post('/api/runs/round-http/execution',json={'role':'correction','round':1}).status_code==400
            # Issue #187: the session's progress export records the phase as runtime_status; the tools stay closed.
            recorded=engine.read(workspace/'run.json')
            assert (recorded['status'],recorded['runtime_status'])==('in_progress','correcting')
            for action,body in (('discover',{'plan':{}}),('acquire',{'ids':['W2']}),('finalize',{'investigation':{}})):
                refused=client.post('/api/runs/round-http/tools/'+action,json=body)
                assert refused.status_code==400 and refused.json()['error'].startswith(action+' is closed during correction: '),refused.text
    finally:
        server.shutdown();server.server_close();thread.join(5)



def test_actual_api_worker_runs_research_correction_fresh_audit_and_retains_history(native):
    from test_corrections import prepared, response, audit_for
    from canary import corrections, compact_bundle
    pool,home,root=native
    fixture=root/'fixture';fixture.mkdir();payload,_=prepared(fixture,rounds=1)
    raw=json.loads((fixture/'investigation.json').read_text())
    corrected=response(payload)
    proposed,_=corrections.apply(payload,engine.read(fixture/'audit.json',{'claim_checks':[]} ),corrected)
    # A fresh bound projection supplies the second auditor's current claim IDs.
    revised=engine.bind_payload(fixture,investigation=proposed)
    names=['question-plan.json','selection.json','reviewed-papers.json','papers/W1.txt','texts/W1.txt',
        'passages/W1.jsonl','download-receipts/W1.json']
    engine.write(home/'scripted.json',{'research':[{'files':{n:(fixture/n).read_text() for n in names},'response':raw}],
        'audit':[{'response':audit_for(payload,'correction_needed')},{'response':audit_for(revised)}],
        'correction':[{'response':corrected}]})
    server=dashboard.make_server(0,registry=root/'registry.json',root=root/'data')
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth.close();supervisor.auth=pool.auth;supervisor.execution=pool;pool.can_start=supervisor._can_execute
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=20,trust_env=False) as client:
            started=client.post('/api/runs',json={'spec':{'question':'Synthetic fixture question?',
                'retrieval':{'target_papers':1},'audit':{'max_correction_rounds':1}}})
            assert started.status_code==201,started.text
            key=started.json()['key']
            def finished():
                row=client.get('/api/runs/'+key).json()
                return row if not row.get('live',True) else None
            row=eventually(finished,timeout=25)
            result=client.get('/api/runs/'+key+'/bundle').json()
            graph=result['research']['answer']
            assert graph['verification']['failure'] is None,(row,result)
            assert graph['verification']['correction_rounds']==1
            assert graph['claims'][0]['text'].startswith('Revised')
            assert graph['source_checks'][0]['assessment']=='supported'
            assert graph['claim_revisions'][0]['investigation']==raw
            assert result['bundle']['retention']['mode']=='bundle_only'
            roles=result['research']['profile']['roles']
            assert sum(r['tokens']['input_tokens'] for r in roles.values())==400
            recorded=calls(home)
            assert len([c for c in recorded if c['method']=='thread/start'])==3
            assert len([c for c in recorded if c['method']=='thread/resume'])==1
            assert all(c['params']['model']=='gpt-6.1-sol' for c in recorded if c['method']=='thread/start')
    finally:
        server.shutdown();server.server_close();thread.join(5)


@pytest.mark.parametrize('retry_fixes',[True,False],ids=['retry_accepted','retry_still_invalid'])
def test_actual_api_worker_correction_cannot_acquire_and_gets_one_retry_naming_its_passages(native,retry_fixes):
    # Issue #187: a correction session acquired a paper with canary.tools_cli and cited W…-r001-table5 passage IDs.
    import hashlib,shlex
    from copy import deepcopy
    from test_corrections import two_claims,response,audit_for
    from canary import audit_validation,corrections
    pool,home,root=native
    fixture=root/'fixture';fixture.mkdir();payload,_,first=two_claims(fixture)
    raw=json.loads((fixture/'investigation.json').read_text())
    corrected=response(payload);invalid,still=deepcopy(corrected),deepcopy(corrected)
    invalid['responses'][0]['passage_ids']=['W1-r001-table5'];still['responses'][0]['passage_ids']=['W1-r001-figure9']
    proposed,_=corrections.apply(payload,audit_validation.bind(payload,first),corrected)
    revised=engine.bind_payload(fixture,investigation=proposed)
    # As a session does, it acquires after its progress export has recorded the correcting phase.
    acquire=('for i in $(seq 250); do grep -q \'"runtime_status": "correcting"\' run.json && break; sleep .02; done; '
             +shlex.quote(sys.executable)+' -m canary.tools_cli --run "$RUN_KEY" acquire --ids W2')
    names=['question-plan.json','selection.json','reviewed-papers.json','papers/W1.txt','texts/W1.txt',
        'passages/W1.jsonl','download-receipts/W1.json']
    engine.write(home/'scripted.json',{'research':[{'files':{n:(fixture/n).read_text() for n in names},'response':raw}],
        'audit':[{'response':first},{'response':audit_for(revised)}],
        'correction':[{'commands':[acquire],'response':invalid},{'response':corrected if retry_fixes else still}]})
    server=dashboard.make_server(0,registry=root/'registry.json',root=root/'data')
    supervisor=server.RequestHandlerClass.supervisor
    supervisor.auth.close();supervisor.auth=pool.auth;supervisor.execution=pool;pool.can_start=supervisor._can_execute
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=20,trust_env=False) as client:
            started=client.post('/api/runs',json={'spec':{'question':'Synthetic fixture question?',
                'retrieval':{'target_papers':1},'audit':{'max_correction_rounds':1}}})
            assert started.status_code==201,started.text
            key=started.json()['key']
            eventually(lambda:not client.get('/api/runs/'+key).json().get('live',True),timeout=25)
            graph=client.get('/api/runs/'+key+'/bundle').json()['research']['answer']
            exported=client.get('/api/runs/'+key+'/exports/resolvent').json()
    finally:
        server.shutdown();server.server_close();thread.join(5)
    # The tool refuses the acquisition by name, and the run continues.
    [attempt]=[json.loads(line) for line in (home/'commands.jsonl').read_text().splitlines()]
    assert attempt['exit_code']==1 and 'acquire is closed during correction: ' in attempt['stderr'],attempt
    # The invalid response goes back to the same session once, naming the exact passage IDs.
    problem='Correction response rejected: C1 cites passage IDs that are not source passages of this run: '
    [correction]=[r for r in graph['claim_revisions'] if r['kind']=='correction']
    retry=correction['receipt']['response_retry']
    assert (retry['problem'],retry['resumed'],retry['role'])==(problem+'W1-r001-table5',True,'correction_001')
    assert retry['rejected_response_sha256']==hashlib.sha256(json.dumps(invalid).encode()).hexdigest()
    jsonschema.validate(correction['receipt'],api_contract.schemas()['ExecutionReceipt'])
    recorded=calls(home)
    turns=[c['params'] for c in recorded if c['method']=='turn/start' and 'responses' in c['params']['outputSchema']['properties']]
    assert len(turns)==2 and turns[0]['threadId']==turns[1]['threadId']
    assert problem+'W1-r001-table5' in turns[1]['input'][0]['text'] and 'This is the only retry' in turns[1]['input'][0]['text']
    audits=len([c for c in recorded if c['method']=='thread/start'])-1
    if retry_fixes:
        assert graph['verification']['failure'] is None and 'stopped' not in graph['verification'] and audits==2
        assert [c['id'] for c in graph['claims']]==['C1','C2'] and graph['claims'][0]['text'].startswith('Revised')
    else:
        # The round still fails: the claim the audit passed stays supported; only the flagged one is withheld.
        failure='ValueError: '+problem+'W1-r001-figure9'
        assert (graph['verification']['failure'],correction['status'],audits)==(failure,'failed',1)
        assert graph['verification']['stopped']=={'kind':'correction','round':1,'status':'failed','audit_round':0,'changed_inputs':[]}
        assert [c['id'] for c in graph['claims']]==['C2'] and [r['claim']['id'] for r in graph['withheld_claims']]==['C1']
        assert graph['withheld_claims'][0]['reason'].startswith('Verification stopped ('+failure+'). ')
        assert [c['statement'] for c in exported['claims']]==[graph['claims'][0]['text']]
        assert corrections.stopped_text(graph['verification']) in exported['result']['limitations']
    run_dashboard(root,graph,r'''
const html=verificationHtml(graph);
assert.match(html,/Correction round 1 · Canary could not accept the first response\. Correction response rejected: C1 cites passage IDs that are not source passages of this run: W1-r001-table5\. The same session was asked once to fix it;/);
if(graph.verification.failure){
 assert.match(html,/Correction round 1 did not finish\. Only claims audit round 0 passed, unchanged since, stay active; every other claim was withheld\./);
 assert.match(html,/the round then stopped: ValueError: Correction response rejected: C1 cites passage IDs that are not source passages of this run: W1-r001-figure9/);
 assert.match(withheldClaimsHtml(graph),/C1 · withheld/);
} else assert.match(html,/its corrected response was accepted\./);
''')



def test_failed_research_archival_can_resume_the_still_open_thread(native):
    pool,home,root=native
    (home/'fail-archive').touch()
    _,first=execute(pool,root,'archive-failed-resume')
    assert first['cleanup']['archived'] is False
    (home/'fail-archive').unlink()
    workspace=root/'archive-failed-resume'
    _,resumed=engine.scientific_session(workspace,'correction','gpt-6.1-sol','xhigh','Synthetic continuation',{},
        executable=str(root/'codex'),execution=pool,run_key='archive-failed-resume',round_index=1,
        resume_thread_id=first['thread_id'],expected_backend=first['backend'],resume_archived=False)
    assert resumed['thread_id']==first['thread_id']
    assert not any(c['method']=='thread/unarchive' for c in calls(home))
    run_dashboard(root,{'run':{'selection_method':'coverage-guided'},'profile':{'roles':{'correction_001':resumed}}},"""
assert.match(coverageTokenHtml(graph),/resumed research conversation/);
""")
