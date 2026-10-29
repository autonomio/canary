"""Offline acquisition replay. Set CANARY_REPLAY_SOURCE to a directory of PDFs.

Run with the baseline/candidate src on PYTHONPATH in separate one-vCPU Linux
containers; source PDFs must be mounted read-only. No model or network calls.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time

if sys.argv[1] == 'worker':
    import httpx
    from canary import coverage_runtime as r, coverage_sources as s
    from canary.candidate_index import CandidateIndex
    workspace = Path(sys.argv[2]); workspace.mkdir()
    files = sorted(Path(os.environ['CANARY_REPLAY_SOURCE']).glob('*.pdf'), key=lambda p: (p.stat().st_size, p.name))
    chosen = files[len(files)//2-6:len(files)//2+6]
    assert len(chosen) == 12
    refs = {p.stem: p for p in chosen}
    with CandidateIndex(workspace) as index:
        index.add_page([{'id': 'https://openalex.org/' + key, 'display_name': 'Replay ' + key,
            'primary_location': {'is_oa': True, 'pdf_url': 'https://example.org/' + key}}
            for key in refs], 'fixture')
    original, calls, guard = s._pdf_text, [], threading.Lock()
    def count(raw):
        with guard: calls.append(hashlib.sha256(raw).hexdigest())
        return original(raw)
    s._pdf_text = count
    def handler(request):
        return httpx.Response(200, content=refs[request.url.path[1:]].read_bytes())
    def resolver(host, port, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', port))]
    def fetch(work, transport):
        return s.public_work_download(work, transport, resolver)
    started = time.monotonic()
    rows = r.acquire(workspace, list(refs), fetch=fetch, transport=httpx.MockTransport(handler))
    assert all(row['status'] == 'source_retrieved' for row in rows), rows
    own, child = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({'wall': time.monotonic()-started, 'sources': len(rows), 'conversions': len(calls),
        'cpu': own.ru_utime+own.ru_stime+child.ru_utime+child.ru_stime,
        'reused': sum(row.get('validated_text_reused', False) for row in rows),
        'body_hashes': {row['work_id']: row['reference']['body_sha256'] for row in rows},
        'text_hashes': {row['work_id']: row['reference']['text_sha256'] for row in rows}}), flush=True)
else:
    groups = int(sys.argv[1]); root = Path('/tmp/acquisition-replay'); root.mkdir()
    stop, samples = threading.Event(), []
    def sample():
        while not stop.wait(.025):
            values = dict(line.split() for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines())
            samples.append(int(values['anon']))
    monitor = threading.Thread(target=sample); monitor.start(); started = time.monotonic()
    def one(i):
        done = subprocess.run([sys.executable, __file__, 'worker', str(root/str(i))],
                              capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout.splitlines()[-1])
    try:
        with ThreadPoolExecutor(max_workers=groups) as pool: results = list(pool.map(one, range(groups)))
    finally:
        stop.set(); monitor.join()
    assert all(row['text_hashes'] == results[0]['text_hashes'] for row in results)
    print(json.dumps({'groups': groups, 'wall': time.monotonic()-started,
        'cpu': sum(row['cpu'] for row in results), 'sources': sum(row['sources'] for row in results),
        'conversions': sum(row['conversions'] for row in results), 'reused': sum(row['reused'] for row in results),
        'peak_anon': max(samples), 'body_hashes': results[0]['body_hashes'], 'text_hashes': results[0]['text_hashes'],
        'container_memory_peak': int(Path('/sys/fs/cgroup/memory.peak').read_text()), 'scientific_run': False}), flush=True)
