"""One PDF parser at a time across workers sharing the service data directory."""
from contextlib import contextmanager
import fcntl

from .runtime_paths import data_root


class CopySuperseded(RuntimeError):
    """Another usable copy of this paper was selected."""


@contextmanager
def conversion_slot(cancelled=None):
    # Open independently for every caller: flock must serialize threads as well
    # as processes. Never unlink the file; a replacement would split the lock.
    path = data_root() / 'runtime' / 'pdf-conversion.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as slot:
        if cancelled is None:
            fcntl.flock(slot, fcntl.LOCK_EX)
        else:
            # Wait only while capacity is occupied. A losing copy can release
            # its body when another wins, without first taking the slot.
            while not cancelled.is_set():
                try:
                    fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    cancelled.wait(.01)
            else:
                raise CopySuperseded()
        try:
            if cancelled is not None and cancelled.is_set():
                raise CopySuperseded()
            yield slot.fileno()
        finally:
            fcntl.flock(slot, fcntl.LOCK_UN)
