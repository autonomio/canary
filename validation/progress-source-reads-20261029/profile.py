"""Offline replay: mount an existing full run read-only at CANARY_REPLAY_SOURCE.

Run baseline and candidate in separate one-vCPU Linux containers. Only temporary
copies are modified. No inference, external network or production API is used.
"""
import hashlib
import inspect
import json
import os
from pathlib import Path
import resource
import shutil
import statistics
import threading
import time

import httpx
from canary import coverage_runtime as r, dashboard, runs

root = Path('/tmp/progress-profile'); root.mkdir()
work = root / 'research'
shutil.copytree(Path(os.environ['CANARY_REPLAY_SOURCE']), work)
for name in ('profile.json','research-receipt.json','audit-receipt.json'):
    (work/name).unlink(missing_ok=True)  # Simulate an active session, not old completed-role usage.
run = r.read(work / 'run.json'); run.update(status='researching', runtime_status='researching')
r.write(work / 'run.json', run)
usage = {'available':True, 'totals':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10},
         'receipts':[], 'response_count':1}
r.native_usage = lambda *args: usage
state = {}
options = {'progress_state':state} if 'progress_state' in inspect.signature(r.refresh_committed_progress).parameters else {}
def refresh(elapsed, **kw):
    return r.refresh_committed_progress(work, 'research', 'gpt-6.1-sol','xhigh','fixture',
        '2026-10-29T00:00:00Z',elapsed, **options, **kw)
for n in range(3): refresh(n)
body_reads = []
original_bytes, original_text = Path.read_bytes, Path.read_text
def count_bytes(path, *args, **kw):
    if path.parent.name in ('papers','texts','passages'): body_reads.append(path.stat().st_size)
    return original_bytes(path,*args,**kw)
def count_text(path, *args, **kw):
    if path.parent.name in ('papers','texts','passages'): body_reads.append(path.stat().st_size)
    return original_text(path,*args,**kw)
Path.read_bytes, Path.read_text = count_bytes, count_text
samples=[]
for n in range(10):
    usage['totals']['input_tokens'] += 1
    wall, cpu = time.monotonic(), time.process_time()
    refresh(10+n)
    samples.append({'wall':time.monotonic()-wall,'cpu':time.process_time()-cpu})
Path.read_bytes, Path.read_text = original_bytes, original_text
refresh(30, finished=True)
assert r.read(work/'run.json')['receipts'][0]['usage']['input_tokens']==110
answer = r.read(work/'answer.json')
answer.pop('profile_summary',None)
science_hash=hashlib.sha256(json.dumps(answer,sort_keys=True).encode()).hexdigest()
server=dashboard.make_server(0, root=root, registry=root/'registry.json')
runs.register_start('fixture','fixture','',kind='question',bundle=str(work),status='researching',path=root/'registry.json')
thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
reference=r.catalog(work)[0]['id']
source_times=[]
def cpu_total():
    own,child=resource.getrusage(resource.RUSAGE_SELF),resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime+own.ru_stime+child.ru_utime+child.ru_stime
cpu=cpu_total()
try:
    with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False) as client:
        for n in range(10):
            body={'reference_id':reference, 'match':None if n%2 else 'methods|results'}
            expected=r.source(work,reference,match=body['match'],record_read=False)
            started=time.monotonic()
            response=client.post('/api/runs/fixture/tools/source',json=body)
            source_times.append(time.monotonic()-started)
            assert response.status_code==200, response.text
            assert response.json()==expected
finally:
    source_cpu=cpu_total()-cpu
    server.shutdown(); server.server_close(); thread.join(5)
print(json.dumps({'progress':{'median_cpu':statistics.median(s['cpu'] for s in samples),
    'median_wall':statistics.median(s['wall'] for s in samples), 'samples':samples,
    'paper_text_passage_bytes_read':sum(body_reads),'refreshes':len(samples)},
    'source_api':{'median_wall':statistics.median(source_times),'samples':source_times,
    'cpu_seconds':source_cpu,'requests':len(source_times),
    'worker_launches':len(list((root/'run-tools/fixture').glob('*/ownership.json'))),
    'results_match_direct_source':True},'final_scientific_graph_sha256':science_hash,
    'scientific_run':False,'provider_calls':0}),flush=True)
