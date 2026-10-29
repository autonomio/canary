"""Real process/thread exclusion and interrupted-owner cleanup; no inference."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
import subprocess
import sys
import time

import pytest

from canary import coverage_sources as sources
from canary.pdf_capacity import conversion_slot


def test_pdf_conversions_share_capacity_across_threads_and_processes(tmp_path):
    worker = r'''
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os, threading, time
from canary import coverage_sources as s
root = Path(os.environ['CANARY_DATA_DIR'])
barrier = threading.Barrier(4)
def parse(raw, fd):
    # Exclusive creation fails immediately if either threads or processes overlap.
    marker = root / 'active-parser'
    with marker.open('x'):
        with (root / 'completed').open('a') as log: log.write('conversion\n')
        time.sleep(.025)
    marker.unlink()
    return 'source text', 1, ()
s._pdf_text_in_slot = parse
def one(_):
    barrier.wait(timeout=5)
    return s._pdf_text(b'%PDF synthetic')
with ThreadPoolExecutor(max_workers=4) as pool:
    assert len(list(pool.map(one, range(4)))) == 4
'''
    env = dict(os.environ, CANARY_DATA_DIR=str(tmp_path))
    def run(_):
        return subprocess.run([sys.executable, '-c', worker], env=env, capture_output=True, text=True, timeout=15)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, range(3)))
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    assert (tmp_path / 'completed').read_text().splitlines() == ['conversion'] * 12
    assert not (tmp_path / 'active-parser').exists()


def test_failed_conversion_releases_capacity(tmp_path, monkeypatch):
    monkeypatch.setenv('CANARY_DATA_DIR', str(tmp_path))
    with pytest.raises(sources.AccessFailure):
        sources._pdf_text(b'not a PDF')
    # Check from another process so a leaked owner cannot silently reacquire.
    result = subprocess.run([sys.executable, '-c',
        'from canary.pdf_capacity import conversion_slot\nwith conversion_slot(): pass'],
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_superseded_copy_leaves_the_queue_while_another_parser_owns_capacity(tmp_path, monkeypatch):
    import threading
    from canary import pdf_capacity
    monkeypatch.setenv('CANARY_DATA_DIR', str(tmp_path))
    cancelled, waiting = threading.Event(), threading.Event()
    original = pdf_capacity.fcntl.flock
    def flock(fd, operation):
        try:
            return original(fd, operation)
        except BlockingIOError:
            waiting.set()
            raise
    monkeypatch.setattr(pdf_capacity.fcntl, 'flock', flock)
    def queued():
        with conversion_slot(cancelled):
            pytest.fail('Superseded copy entered the converter')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with conversion_slot():
            future = pool.submit(queued)
            try:
                assert waiting.wait(5)  # It really attempted the occupied lock.
                cancelled.set()
                with pytest.raises(pdf_capacity.CopySuperseded):
                    future.result(timeout=2)
            finally:
                cancelled.set()


def test_native_converter_retains_slot_if_its_parent_is_killed(tmp_path):
    converter = tmp_path / 'pdftotext'
    converter.write_text('#!' + sys.executable + '\n' + r'''
from pathlib import Path
import os, time
root = Path(os.environ['CANARY_DATA_DIR'])
ready = root / 'native-ready.tmp'
ready.write_text(str(os.getpid()))
ready.replace(root / 'native-ready')
while not (root / 'release-native').exists(): time.sleep(.01)
print('Extractable PDF source text.\f')
''')
    converter.chmod(0o755)
    env = dict(os.environ, CANARY_DATA_DIR=str(tmp_path), PATH=str(tmp_path) + os.pathsep + os.environ['PATH'])
    proc = subprocess.Popen([sys.executable, '-c',
        "from canary.coverage_sources import _pdf_text; _pdf_text(b'%PDF synthetic')"], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / 'native-ready').exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(.01)
        assert int((tmp_path / 'native-ready').read_text()) > 0
        proc.kill(); proc.wait(timeout=5)
        with (tmp_path / 'runtime' / 'pdf-conversion.lock').open('ab') as slot:
            with pytest.raises(BlockingIOError):
                fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (tmp_path / 'release-native').touch()
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        pytest.fail('Native conversion did not release shared capacity')
                    time.sleep(.01)
    finally:
        (tmp_path / 'release-native').touch()
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=5)
        proc.communicate(timeout=5)


def test_separate_service_data_directories_have_separate_slots(tmp_path, monkeypatch):
    monkeypatch.setenv('CANARY_DATA_DIR', str(tmp_path / 'one'))
    with conversion_slot():
        result = subprocess.run([sys.executable, '-c',
            'from canary.pdf_capacity import conversion_slot\nwith conversion_slot(): pass'],
            env=dict(os.environ, CANARY_DATA_DIR=str(tmp_path / 'two')),
            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
