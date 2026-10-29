"""One PDF parser at a time across workers sharing the service data directory."""
from contextlib import contextmanager
import fcntl

from .runtime_paths import data_root


@contextmanager
def conversion_slot():
    # Open independently for every caller: flock must serialize threads as well
    # as processes. Never unlink the file; a replacement would split the lock.
    path = data_root() / 'runtime' / 'pdf-conversion.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as slot:
        fcntl.flock(slot, fcntl.LOCK_EX)
        try:
            yield slot.fileno()
        finally:
            fcntl.flock(slot, fcntl.LOCK_UN)
