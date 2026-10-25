"""Persist native worker ownership and stop its descendants, including new sessions."""
import os
import json
from pathlib import Path
import signal
import subprocess
import time


class RunCancelled(RuntimeError):
    pass


def identity(pid):
    """Birth identity prevents treating a reused PID or zombie as a live worker."""
    try:
        stat = Path(f'/proc/{int(pid)}/stat')
        if Path('/proc').is_dir():
            fields = stat.read_text().rsplit(')', 1)[1].split()
            return None if fields[0] == 'Z' else fields[19]
        result = subprocess.run(['ps', '-p', str(int(pid)), '-o', 'lstart=', '-o', 'stat='],
                                capture_output=True, text=True, timeout=3)
        fields = result.stdout.split()
        return ' '.join(fields[:-1]) if len(fields) >= 6 and not fields[-1].startswith('Z') else None
    except (OSError, ValueError, TypeError, IndexError, subprocess.TimeoutExpired):
        return None


def live(receipt):
    return bool(receipt and receipt.get('identity') and identity(receipt.get('pid')) == receipt['identity'])


def tool_receipts(root, key):
    result = []
    for path in (Path(root)/'run-tools'/key).glob('*/ownership.json'):
        try:
            receipt = json.loads(path.read_text(encoding='utf-8'))
            if live(receipt): result.append(receipt)
        except (OSError, ValueError):
            pass
    return result


def stop_tree(receipt, grace=10):
    if not live(receipt):
        return False
    root = receipt['pid']
    snapshot = subprocess.run(['ps', '-axo', 'pid=,ppid='], capture_output=True, text=True, check=True, timeout=5)
    parents = {}
    for line in snapshot.stdout.splitlines():
        pid, parent = map(int, line.split())
        parents.setdefault(parent, []).append(pid)
    descendants = []
    def walk(pid):
        for child in parents.get(pid, []):
            walk(child)
        descendants.append({'pid': pid, 'identity': identity(pid)})
    walk(root)
    # Signal the owner first so its cancellation handler protects final cleanup.
    # Killing a child first can start normal failure cleanup before the owner is
    # signalled, interrupting its bundle seal halfway through.
    for row in reversed(descendants):
        if live(row):
            try:
                os.kill(row['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(live(row) for row in descendants):
        time.sleep(.05)
    for row in descendants:
        if live(row):
            try:
                os.kill(row['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
    return True
