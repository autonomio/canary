"""Review editions kept alongside the sealed research bundle."""
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
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
    """Share ordinary reads and take an exclusive lock only to recover an interrupted change."""
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
    if not row or row[:2] != (VERSION, bundle_id):
        raise ReviewStorageError('Review history does not match its base bundle')
    recorded = row[2]
    if recorded is not None and (not isinstance(recorded, str) or not re.fullmatch('[0-9a-f]{64}', recorded)):
        raise ReviewStorageError('Invalid checksum in the review history')
    if checksum is not None and recorded != checksum:
        raise ReviewStorageError('Review history does not match its base bundle')
    tail = db.execute('SELECT id FROM entries ORDER BY rowid DESC LIMIT 1').fetchone()
    if row[3] != (tail[0] if tail else INITIAL):
        raise ReviewStorageError('Review history has an inconsistent last entry')
    return row[3]


def subject_id(entry: Record, inherited: str) -> str:
    """Resolve the subject recorded on a decision or inherited from its predecessor."""
    return entry.get('subject_id') or entry['decision'].get('subject_id') or inherited


def _index_subject(subjects: dict[str, tuple[str, str]], identifier: str, entry: Record) -> None:
    try:
        parent = entry.get('replaces')
        original = identifier if parent is None else subjects[parent][0]
        decision = entry['decision']
        subjects[identifier] = subject_id(entry, original), decision['subject']
    except (KeyError, TypeError, AttributeError) as error:
        raise ReviewStorageError('Invalid subject or correction link in the saved history') from error


def _verify_entries(db: sqlite3.Connection, bundle_id: str, *, through: str | None = None,
                    entries: list[Record] | None = None, subjects: dict[str, tuple[str, str]] | None = None) -> None:
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
        if subjects is not None:
            _index_subject(subjects, identifier, entry)
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


def _source_files(root: Path) -> dict[Path, tuple[int, int, int, int]]:
    files = {}
    for name in (NAME, NAME + '-journal', NAME + '-wal'):
        path = root / name
        if path.is_symlink():
            raise ReviewStorageError('Invalid review file')
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files[path] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns)
    return files


def _merge_history(stored: sqlite3.Connection, incoming: sqlite3.Connection, bundle_id: str,
                  incoming_head: str) -> None:
    stored.execute('BEGIN IMMEDIATE')
    head = _header(stored, bundle_id, None)
    _verify_entries(stored, bundle_id)
    saved_anchor = stored.execute('SELECT base_sha256 FROM history').fetchone()[0]
    incoming_anchor = incoming.execute('SELECT base_sha256 FROM history').fetchone()[0]
    if saved_anchor and incoming_anchor and saved_anchor != incoming_anchor:
        raise ReviewConflict('The imported decisions refer to a different compact copy')
    if head != incoming_head and incoming_head != INITIAL and not stored.execute(
            'SELECT 1 FROM entries WHERE id = ?', (incoming_head,)).fetchone():
        if head == INITIAL:
            suffix = incoming.execute('SELECT id, body FROM entries ORDER BY rowid')
        else:
            common = incoming.execute('SELECT rowid FROM entries WHERE id = ?', (head,)).fetchone()
            if common is None:
                raise ReviewConflict('The imported and saved review histories contain conflicting decisions')
            suffix = incoming.execute('SELECT id, body FROM entries WHERE rowid > ? ORDER BY rowid', (common[0],))
        stored.executemany('INSERT INTO entries (id, body) VALUES (?, ?)', suffix)
        stored.execute('UPDATE history SET last_entry = ?', (incoming_head,))
    appendix = _methods(incoming, bundle_id)
    if appendix is not None:
        _store_methods(stored, bundle_id, appendix)
    if saved_anchor is None and incoming_anchor is not None:
        stored.execute('UPDATE history SET base_sha256 = ?', (incoming_anchor,))


def _import_history(root: Path, target: Path, bundle_id: str, checksum: str | None) -> None:
    with tempfile.TemporaryDirectory(dir=target) as temporary:
        snapshot = Path(temporary)
        source_files = _source_files(root)
        for source in source_files:
            shutil.copyfile(source, snapshot / source.name)
        if source_files != _source_files(root):
            raise ReviewStorageError('The imported review changed while it was being copied')
        path = snapshot / NAME
        with _connect(path) as db:
            db.execute('PRAGMA journal_mode=DELETE').fetchone()
        _recover_binding(root, snapshot, bundle_id, checksum)
        with _connect(path, mode='ro') as incoming:
            head = _header(incoming, bundle_id, checksum)
            _verify_entries(incoming, bundle_id)
            if (target / NAME).exists():
                with _connect(target / NAME) as stored, stored:
                    _merge_history(stored, incoming, bundle_id, head)
                return
            _methods(incoming, bundle_id)
        path.replace(target / NAME)


@contextmanager
def _directory(root: Path, bundle_id: str, checksum: str | None, store: Path | None, *,
               write: bool = False, merge: bool = False) -> Iterator[Path]:
    """Open the service's copy, taking the bundle's decisions only on first use or an explicit import."""
    if store is None:
        yield root
        return
    if store.resolve().is_relative_to(root.resolve()):
        raise ReviewStorageError('The decision store must be outside the imported bundle')
    target = store / digest(bundle_id)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReviewStorageError('Could not open the imported decision history') from error
    source = (root / NAME).is_file() or (root / NAME).is_symlink()
    if not write and not merge:
        with locked(target, shared=True):
            path = target / NAME
            ready = path.exists() or not source
            if ready and not path.with_name(NAME + '-journal').exists():
                if checksum is not None and path.exists():
                    with _connect(path, mode='ro') as db:
                        ready = db.execute('SELECT 1 FROM history WHERE base_sha256 IS NULL').fetchone() is None
                if ready:
                    yield target
                    return
    with locked(target):
        if source and (merge or not (target / NAME).exists()):
            try:
                _import_history(root, target, bundle_id, checksum)
            except OSError as error:
                raise ReviewStorageError('Could not copy the imported decision history') from error
        yield target


def import_history(workspace: str | Path, store: Path) -> None:
    """Merge the bundle's own decisions into the service's copy when the bundle is imported."""
    root = Path(workspace).resolve()
    with reading(root):
        source = (root / NAME).is_file() or (root / NAME).is_symlink()
        try:
            bundle_id, checksum = _base(root)
        except ValueError:
            if source:
                raise
            return
        if not source and not (store / digest(bundle_id) / NAME).is_file():
            return
        with _directory(root, bundle_id, checksum, store, merge=True) as directory:
            _recover_binding(root, directory, bundle_id, checksum)
            path = directory / NAME
            if checksum is not None and path.is_file():
                with _connect(path, mode=_read_mode(path)) as db:
                    saved = db.execute('SELECT base_sha256 FROM history').fetchone()
                if saved and saved[0] not in (None, checksum):
                    raise ReviewConflict('The saved decisions refer to a different compact copy')


def _recover_binding(root: Path, directory: Path, bundle_id: str, checksum: str | None) -> None:
    if checksum is None or not (directory / NAME).is_file():
        return
    if directory != root or (root / '.run-reservation').is_file():
        path = directory / NAME
        with _connect(path, mode=_read_mode(path)) as db:
            unbound = db.execute('SELECT 1 FROM history WHERE base_sha256 IS NULL').fetchone()
        if unbound:
            try:
                manifest = compact_bundle.manifest(root)
                if manifest['id'] != bundle_id:
                    raise ValueError('Bundle ID differs from the stored contribution')
            except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
                raise ReviewStorageError('Cannot verify the stored contribution before linking decisions') from error
            bind(directory, bundle_id, checksum)


def edition(workspace: str | Path, last_entry: str | None = None, *, store: Path | None = None) -> Record:
    """Read decisions through the requested entry, or through the most recent one."""
    root = Path(workspace).resolve()
    with reading(root):
        bundle_id, checksum = _base(root)
        with _directory(root, bundle_id, checksum, store) as directory:
            path = directory / NAME
            if not path.exists():
                if last_entry not in (None, INITIAL):
                    raise UnknownEdition('Unknown review edition')
                return dict(version=VERSION, bundle_id=bundle_id, base_sha256=checksum, last_entry=INITIAL, entries=[])
            _recover_binding(root, directory, bundle_id, checksum)
            with _connect(path, mode=_read_mode(path)) as db:
                return _edition(db, bundle_id, checksum, last_entry)


def _initialize(db: sqlite3.Connection, bundle_id: str, checksum: str | None) -> None:
    db.execute('CREATE TABLE history(version INTEGER, bundle_id TEXT, base_sha256 TEXT, last_entry TEXT)')
    db.execute('CREATE TABLE entries(id TEXT PRIMARY KEY, body TEXT NOT NULL)')
    db.execute('INSERT INTO history VALUES (?, ?, ?, ?)', (VERSION, bundle_id, checksum, INITIAL))


def _correction_subject(db: sqlite3.Connection, subjects: dict[str, tuple[str, str]], replaces: str,
                        decision: Record) -> str:
    if db.execute("SELECT 1 FROM entries WHERE json_extract(body, '$.replaces') = ?", (replaces,)).fetchone():
        raise ReviewConflict('This decision has already been corrected')
    if replaces not in subjects:
        raise ValueError('Unknown review decision to replace')
    subject_id, name = subjects[replaces]
    if 'subject_id' in decision and decision['subject_id'] != subject_id:
        raise ValueError('A correction must identify the same subject as the original decision')
    if decision['subject'] != name and decision.get('subject_id') != subject_id:
        raise ValueError('Supply the original subject ID to change its displayed name')
    return subject_id


def _record(db: sqlite3.Connection, bundle_id: str, checksum: str | None, decision: Record, *,
            request_id: str, expected_last_entry: str, replaces: str | None) -> str:
    previous = _header(db, bundle_id, checksum)
    subjects: dict[str, tuple[str, str]] = {}
    _verify_entries(db, bundle_id, subjects=subjects if replaces is not None or 'subject_id' in decision else None)
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
    entry = dict(previous=previous, request_id=request_id, replaces=replaces,
                 recorded_at=datetime.now(timezone.utc).isoformat(), decision=decision)
    if replaces is not None:
        entry['subject_id'] = _correction_subject(db, subjects, replaces, decision)
    elif 'subject_id' in decision:
        subject_id = decision['subject_id']
        if subject_id in subjects and subjects[subject_id][0] != subject_id:
            raise ValueError('Use the original subject ID for this decision')
        known = {name for key, name in subjects.values() if key == subject_id}
        if known and decision['subject'] not in known:
            raise ValueError('This subject ID is already recorded under a different name')
        entry['subject_id'] = decision['subject_id']
    identifier = digest([bundle_id, entry])
    db.execute('INSERT INTO entries VALUES (?, ?)', (identifier, json.dumps(entry, ensure_ascii=False)))
    db.execute('UPDATE history SET last_entry = ?', (identifier,))
    return identifier


def append(workspace: str | Path, decision: Record, *, request_id: str, expected_last_entry: str,
           replaces: str | None = None, store: Path | None = None) -> str:
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
        with _directory(root, bundle_id, checksum, store, write=True) as directory:
            _recover_binding(root, directory, bundle_id, checksum)
            path = directory / NAME
            new = not path.exists()
            target = directory / ('.' + NAME + '.tmp') if new else path
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


def _methods(db: sqlite3.Connection, bundle_id: str) -> Record | None:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='methods'").fetchone():
        return None
    row = db.execute('SELECT body, sha256 FROM methods WHERE id=1').fetchone()
    if row is None:
        raise ReviewStorageError('Missing saved methods appendix')
    value = json.loads(row[0])
    if digest([bundle_id, value]) != row[1]:
        raise ReviewStorageError('The saved methods appendix has changed')
    return value


def _store_methods(db: sqlite3.Connection, bundle_id: str, value: Record) -> None:
    previous = _methods(db, bundle_id)
    if previous is not None:
        if previous != value:
            raise ReviewConflict('This bundle already has a different methods appendix')
        return
    db.execute('CREATE TABLE methods(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL, sha256 TEXT NOT NULL)')
    db.execute('INSERT INTO methods VALUES (1, ?, ?)', (json.dumps(value, ensure_ascii=False), digest([bundle_id, value])))


def has_methods(workspace: Path) -> bool:
    """Check whether the bundle already retains its methods appendix."""
    path = workspace / NAME
    if not path.exists():
        return False
    with _connect(path, mode=_read_mode(path)) as db:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='methods'").fetchone() is not None


def retained_methods(workspace: Path, bundle_id: str, checksum: str, store: Path | None) -> Record | None:
    """Read the compact review under the caller's bundle lock."""
    with _directory(workspace, bundle_id, checksum, store) as directory:
        path = directory / NAME
        if not path.exists():
            return None
        _recover_binding(workspace, directory, bundle_id, checksum)
        with _connect(path, mode=_read_mode(path)) as db:
            _header(db, bundle_id, checksum)
            result = _methods(db, bundle_id)
    if result is None and store is not None and has_methods(workspace):
        with _connect(workspace / NAME, mode=_read_mode(workspace / NAME)) as source:
            _header(source, bundle_id, checksum)
            result = _methods(source, bundle_id)
        with _directory(workspace, bundle_id, checksum, store, write=True) as directory:
            bind(directory, bundle_id, checksum, result)
    return result


def bind(workspace: Path, bundle_id: str, checksum: str, appendix: Record | None = None) -> None:
    """Save the appendix under the bundle lock before releasing working files."""
    path = workspace / NAME
    new = not path.exists()
    if new and appendix is None:
        return
    target = workspace / ('.' + NAME + '.tmp') if new else path
    if new:
        target.unlink(missing_ok=True)
    try:
        with _connect(target, mode='rwc' if new else 'rw') as db, db:
            db.execute('BEGIN IMMEDIATE')
            if new:
                _initialize(db, bundle_id, None)
            stored = db.execute('SELECT base_sha256 FROM history').fetchone()
            if not stored or stored[0] not in (None, checksum):
                raise ReviewStorageError('Review history is bound to another contribution')
            _header(db, bundle_id, stored[0])
            _verify_entries(db, bundle_id)
            if appendix is not None:
                _store_methods(db, bundle_id, appendix)
            else:
                _methods(db, bundle_id)
            db.execute('UPDATE history SET base_sha256 = ?', (checksum,))
        if new:
            target.replace(path)
    finally:
        if new:
            target.unlink(missing_ok=True)


def archive(workspace: Path, output: ZipFile, manifest: Mapping[str, Any], *, store: Path | None = None) -> None:
    """Add the decision database and the same edition in readable JSON to the ZIP."""
    checksum = (workspace / compact_bundle.CHECKSUM).read_text().strip() if compact_bundle.exists(workspace) else None
    with _directory(workspace, manifest['id'], checksum, store) as directory:
        path = directory / NAME
        if not path.exists():
            return
        _recover_binding(workspace, directory, manifest['id'], checksum)
        with _connect(path, mode=_read_mode(path)) as db:
            db.execute('BEGIN')
            record = _edition(db, manifest['id'], checksum, None)
            if db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal':
                with tempfile.TemporaryDirectory() as temporary:
                    snapshot = Path(temporary) / NAME
                    with closing(sqlite3.connect(snapshot)) as copy:
                        db.backup(copy)
                        copy.execute('PRAGMA journal_mode=DELETE')
                    output.write(snapshot, NAME)
            else:
                output.write(path, NAME)
        output.writestr('review-decisions.json', json.dumps(record, ensure_ascii=False))
