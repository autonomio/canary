"""Review editions kept alongside the sealed research bundle."""
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any
from zipfile import ZipFile

from . import compact_bundle, research_bundle, review_decisions
from .research_spec import digest

NAME = 'review-decisions.sqlite3'
DATABASE_FILES = frozenset(NAME + suffix for suffix in ('', '-journal', '-shm', '-wal'))
INITIAL = 'initial'
VERSION = 1
Record = dict[str, Any]


class ReviewConflict(ValueError):
    """The submission conflicts with a decision already recorded."""


class UnknownEdition(KeyError):
    """The requested edition does not occur in the saved history."""


class ReviewStorageError(ValueError):
    """The saved bundle or decision history could not be read or verified."""


@contextmanager
def locked(workspace: Path, *, shared: bool = False) -> Iterator[None]:
    """Hold the bundle lock while reading, recording or sealing decisions."""
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def reading(workspace: Path) -> Iterator[None]:
    """Share ordinary reads and take an exclusive lock for recovery."""
    with locked(workspace, shared=True):
        pending = compact_bundle.exists(workspace) and (workspace / '.run-reservation').is_file()
        if not pending and not (workspace / (NAME + '-journal')).exists():
            yield
            return
    with locked(workspace):
        yield


def _base(workspace: Path) -> tuple[str, str | None]:
    try:
        if compact_bundle.exists(workspace):
            contribution = compact_bundle.read(workspace)
            bundle_id = contribution['method_metadata']['bundle_id']
            checksum = (workspace / compact_bundle.CHECKSUM).read_text().strip()
        else:
            bundle_id = research_bundle.read(workspace)['id']
            checksum = None
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        raise ReviewStorageError('Cannot verify the base research bundle') from error
    if not isinstance(bundle_id, str):
        raise ReviewStorageError('Invalid base bundle ID')
    if not bundle_id:
        raise ValueError('Review decisions require a versioned research bundle')
    return bundle_id, checksum


@contextmanager
def _connect(path: Path, *, mode: str = 'rw') -> Iterator[sqlite3.Connection]:
    if path.is_symlink():
        raise ReviewStorageError('Invalid review file')
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=' + mode, uri=True)) as db:
            yield db
    except sqlite3.DatabaseError as error:
        raise ReviewStorageError('Cannot read or update the review file') from error


def _header(db: sqlite3.Connection, bundle_id: str, checksum: str | None) -> str:
    row = db.execute('SELECT version, bundle_id, base_sha256, last_entry FROM history').fetchone()
    if not row or row[:3] != (VERSION, bundle_id, checksum):
        raise ReviewStorageError('Review history does not match its base bundle')
    tail = db.execute('SELECT id FROM entries ORDER BY rowid DESC LIMIT 1').fetchone()
    if row[3] != (tail[0] if tail else INITIAL):
        raise ReviewStorageError('Review history has an inconsistent last entry')
    return row[3]


def _verify_entries(db: sqlite3.Connection, bundle_id: str, *, through: str | None = None,
                    entries: list[Record] | None = None) -> None:
    if through == INITIAL:
        return
    previous = INITIAL
    for identifier, body in db.execute('SELECT id, body FROM entries ORDER BY rowid'):
        try:
            entry = json.loads(body)
        except (TypeError, ValueError) as error:
            raise ReviewStorageError('Unreadable decision in the saved history') from error
        if not isinstance(entry, dict) or entry.get('previous') != previous or digest([bundle_id, entry]) != identifier:
            raise ReviewStorageError('Review decision changed since it was recorded')
        if entries is not None:
            entries.append(dict(id=identifier, **entry))
        previous = identifier
        if through == identifier:
            return
    if through is not None:
        raise UnknownEdition('Unknown review edition')


def _edition(db: sqlite3.Connection, bundle_id: str, checksum: str | None, last_entry: str | None) -> Record:
    head = _header(db, bundle_id, checksum)
    if last_entry is None:
        last_entry = head
    entries: list[Record] = []
    _verify_entries(db, bundle_id, through=last_entry, entries=entries)
    return dict(version=VERSION, bundle_id=bundle_id, base_sha256=checksum, last_entry=last_entry, entries=entries)


def _read_mode(path: Path) -> str:
    return 'rw' if path.with_name(path.name + '-journal').exists() else 'ro'


def _recover_binding(workspace: Path, bundle_id: str, checksum: str | None) -> None:
    path = workspace / NAME
    if checksum is None or not path.is_file() or not (workspace / '.run-reservation').is_file():
        return
    with _connect(path, mode=_read_mode(path)) as db:
        unbound = db.execute('SELECT 1 FROM history WHERE base_sha256 IS NULL').fetchone()
    if unbound:
        try:
            if compact_bundle.manifest(workspace)['id'] != bundle_id:
                raise ValueError('Bundle ID differs from the stored contribution')
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
            raise ReviewStorageError('Cannot verify the stored contribution before linking decisions') from error
        bind(workspace, bundle_id, checksum)


def edition(workspace: str | Path, last_entry: str | None = None) -> Record:
    """Read decisions through the requested entry, or through the most recent one."""
    root = Path(workspace).resolve()
    with reading(root):
        bundle_id, checksum = _base(root)
        path = root / NAME
        if not path.exists():
            if last_entry not in (None, INITIAL):
                raise UnknownEdition('Unknown review edition')
            return dict(version=VERSION, bundle_id=bundle_id, base_sha256=checksum, last_entry=INITIAL, entries=[])
        _recover_binding(root, bundle_id, checksum)
        with _connect(path, mode=_read_mode(path)) as db:
            return _edition(db, bundle_id, checksum, last_entry)


def _initialize(db: sqlite3.Connection, bundle_id: str, checksum: str | None) -> None:
    db.execute('CREATE TABLE history(version INTEGER, bundle_id TEXT, base_sha256 TEXT, last_entry TEXT)')
    db.execute('CREATE TABLE entries(id TEXT PRIMARY KEY, body TEXT NOT NULL)')
    db.execute('INSERT INTO history VALUES (?, ?, ?, ?)', (VERSION, bundle_id, checksum, INITIAL))


def _record(db: sqlite3.Connection, bundle_id: str, checksum: str | None, decision: Record, *,
            request_id: str, expected_last_entry: str, replaces: str | None) -> str:
    previous = _header(db, bundle_id, checksum)
    _verify_entries(db, bundle_id)
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS review_requests ON entries(json_extract(body, '$.request_id'))")
    db.execute("CREATE INDEX IF NOT EXISTS review_replaces ON entries(json_extract(body, '$.replaces'))")
    saved = db.execute("SELECT id, body FROM entries WHERE json_extract(body, '$.request_id') = ?",
                       (request_id,)).fetchone()
    if saved:
        entry = json.loads(saved[1])
        if (entry['decision'], entry['replaces'], entry['previous']) != (decision, replaces, expected_last_entry):
            raise ReviewConflict('This request ID already belongs to a different submission')
        return saved[0]
    if expected_last_entry != previous:
        raise ReviewConflict('A newer review edition exists. Read it before submitting this decision.')
    if replaces is not None:
        if db.execute("SELECT 1 FROM entries WHERE json_extract(body, '$.replaces') = ?", (replaces,)).fetchone():
            raise ReviewConflict('This decision has already been corrected')
        if not db.execute('SELECT 1 FROM entries WHERE id = ?', (replaces,)).fetchone():
            raise ValueError('Unknown review decision to replace')
    entry = dict(previous=previous, request_id=request_id, replaces=replaces,
                 recorded_at=datetime.now(timezone.utc).isoformat(), decision=decision)
    identifier = digest([bundle_id, entry])
    db.execute('INSERT INTO entries VALUES (?, ?)', (identifier, json.dumps(entry, ensure_ascii=False)))
    db.execute('UPDATE history SET last_entry = ?', (identifier,))
    return identifier


def append(workspace: str | Path, decision: Record, *, request_id: str, expected_last_entry: str,
           replaces: str | None = None) -> str:
    """Record a decision and return the same entry ID when a client retries it."""
    review_decisions.validate(decision)
    if not isinstance(request_id, str) or not re.fullmatch(r'\S{1,128}', request_id):
        raise ValueError('Expected a request ID of 1 to 128 non-whitespace characters')
    if not isinstance(expected_last_entry, str) or not expected_last_entry:
        raise ValueError('Expected the last entry of the edition being amended')
    if replaces is not None and not isinstance(replaces, str):
        raise ValueError('Expected an entry ID to replace')
    if decision['status'] == 'modified' and replaces is None:
        raise ValueError('A modified decision must name the entry it replaces')
    root = Path(workspace).resolve()
    with locked(root):
        bundle_id, checksum = _base(root)
        _recover_binding(root, bundle_id, checksum)
        path = root / NAME
        new = not path.exists()
        target = root / ('.' + NAME + '.tmp') if new else path
        if new:
            target.unlink(missing_ok=True)
        try:
            with _connect(target, mode='rwc' if new else 'rw') as db, db:
                db.execute('BEGIN IMMEDIATE')
                if new:
                    _initialize(db, bundle_id, checksum)
                identifier = _record(db, bundle_id, checksum, decision, request_id=request_id,
                                     expected_last_entry=expected_last_entry, replaces=replaces)
            if new:
                target.replace(path)
        finally:
            if new:
                target.unlink(missing_ok=True)
    return identifier


def bind(workspace: Path, bundle_id: str, checksum: str) -> None:
    """Verify the recorded decisions and link them to the stored contribution."""
    path = workspace / NAME
    if not path.exists():
        return
    with _connect(path) as db, db:
        db.execute('BEGIN IMMEDIATE')
        stored = db.execute('SELECT base_sha256 FROM history').fetchone()
        if not stored or stored[0] not in (None, checksum):
            raise ReviewStorageError('Review history is bound to another contribution')
        _header(db, bundle_id, stored[0])
        _verify_entries(db, bundle_id)
        if stored[0] is None:
            db.execute('UPDATE history SET base_sha256 = ?', (checksum,))


def archive(workspace: Path, output: ZipFile, manifest: Record) -> None:
    """Add the decision database and the same edition in readable JSON to the ZIP."""
    path = workspace / NAME
    if not path.exists():
        return
    checksum = (workspace / compact_bundle.CHECKSUM).read_text().strip() if compact_bundle.exists(workspace) else None
    _recover_binding(workspace, manifest['id'], checksum)
    with _connect(path, mode=_read_mode(path)) as db:
        db.execute('BEGIN')
        record = _edition(db, manifest['id'], checksum, None)
        if db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal':
            with tempfile.TemporaryDirectory() as directory:
                snapshot = Path(directory) / NAME
                with closing(sqlite3.connect(snapshot)) as copy:
                    db.backup(copy)
                    copy.execute('PRAGMA journal_mode=DELETE')
                output.write(snapshot, NAME)
        else:
            output.write(path, NAME)
    output.writestr('review-decisions.json', json.dumps(record, ensure_ascii=False))
