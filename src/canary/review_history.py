"""Review editions kept alongside the sealed research bundle."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
import re
from pathlib import Path
import sqlite3

from . import compact_bundle, research_bundle, review_decisions
from .research_spec import digest

NAME = 'review-decisions.sqlite3'
INITIAL = 'initial'
VERSION = 1


class ReviewConflict(ValueError):
    pass


@contextmanager
def locked(workspace, *, shared=False):
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _base(workspace):
    if compact_bundle.exists(workspace):
        contribution = compact_bundle.read(workspace)
        return contribution['method_metadata']['bundle_id'], (workspace / compact_bundle.CHECKSUM).read_text().strip()
    bundle = research_bundle.read(workspace)
    if not bundle['id']:
        raise ValueError('Review decisions require a versioned research bundle')
    return bundle['id'], None


@contextmanager
def _connect(path, *, mode='rw'):
    if path.is_symlink():
        raise ValueError('Invalid review file')
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True)) as db:
            yield db
    except sqlite3.DatabaseError as error:
        raise ValueError('Cannot read or update the review file: ' + str(error)) from error


def _header(db, bundle_id, checksum):
    row = db.execute('SELECT version, bundle_id, base_sha256, last_entry FROM history').fetchone()
    if not row or row[:3] != (VERSION, bundle_id, checksum):
        raise ValueError('Review history does not match its base bundle')
    tail = db.execute('SELECT id FROM entries ORDER BY rowid DESC LIMIT 1').fetchone()
    if row[3] != (tail[0] if tail else INITIAL):
        raise ValueError('Review history has an inconsistent last entry')
    return row[3]


def _edition(db, bundle_id, checksum, last_entry=None):
    head = _header(db, bundle_id, checksum)
    if last_entry is None:
        last_entry = head
    previous = INITIAL
    entries = []
    if last_entry != INITIAL:
        for identifier, body in db.execute('SELECT id, body FROM entries ORDER BY rowid'):
            entry = json.loads(body)
            if entry['previous'] != previous or digest([bundle_id, entry]) != identifier:
                raise ValueError('Review decision changed since it was recorded')
            entries.append(dict(id=identifier, **entry))
            previous = identifier
            if identifier == last_entry:
                break
    if previous != last_entry:
        raise KeyError('Unknown review edition')
    return dict(version=VERSION, bundle_id=bundle_id, base_sha256=checksum, last_entry=last_entry, entries=entries)


def edition(workspace, last_entry=None):
    workspace = Path(workspace).resolve()
    with locked(workspace, shared=True):
        bundle_id, checksum = _base(workspace)
        if not (workspace / NAME).exists():
            if last_entry not in (None, INITIAL):
                raise KeyError('Unknown review edition')
            return dict(version=VERSION, bundle_id=bundle_id, base_sha256=checksum, last_entry=INITIAL, entries=[])
        with _connect(workspace / NAME, mode='ro') as db:
            return _edition(db, bundle_id, checksum, last_entry)


def append(workspace, decision, *, request_id, expected_last_entry, replaces=None):
    review_decisions.validate(decision)
    if not isinstance(request_id, str) or not re.fullmatch(r'\S{1,128}', request_id):
        raise ValueError('Expected a request ID of 1 to 128 non-whitespace characters')
    if not isinstance(expected_last_entry, str) or not expected_last_entry:
        raise ValueError('Expected the last entry of the edition being amended')
    if replaces is not None and not isinstance(replaces, str):
        raise ValueError('Expected an entry ID to replace')
    if decision['status'] == 'modified' and replaces is None:
        raise ValueError('A modified decision must name the entry it replaces')
    workspace = Path(workspace).resolve()
    with locked(workspace):
        bundle_id, checksum = _base(workspace)
        path = workspace / NAME
        new = not path.exists()
        target = workspace / ('.' + NAME + '.tmp') if new else path
        if new:
            target.unlink(missing_ok=True)
        try:
            with _connect(target, mode='rwc' if new else 'rw') as db, db:
                db.execute('BEGIN IMMEDIATE')
                if new:
                    db.execute('CREATE TABLE history(version INTEGER, bundle_id TEXT, base_sha256 TEXT, last_entry TEXT)')
                    db.execute('CREATE TABLE entries(id TEXT PRIMARY KEY, body TEXT NOT NULL)')
                    db.execute('INSERT INTO history VALUES (?, ?, ?, ?)', (VERSION, bundle_id, checksum, INITIAL))
                previous = _header(db, bundle_id, checksum)
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS review_requests ON entries(json_extract(body, '$.request_id'))")
                db.execute("CREATE INDEX IF NOT EXISTS review_replaces ON entries(json_extract(body, '$.replaces'))")
                saved = db.execute("SELECT id, body FROM entries WHERE json_extract(body, '$.request_id') = ?",
                                   (request_id,)).fetchone()
                if saved:
                    entry = json.loads(saved[1])
                    if digest([bundle_id, entry]) != saved[0]:
                        raise ValueError('Review decision changed since it was recorded')
                    if (entry['decision'], entry['replaces'], entry['previous']) != (decision, replaces, expected_last_entry):
                        raise ReviewConflict('This request ID already belongs to a different submission')
                    return saved[0]
                if expected_last_entry != previous:
                    raise ReviewConflict('A newer review edition exists. Read it before submitting this decision.')
                if replaces and db.execute("SELECT 1 FROM entries WHERE json_extract(body, '$.replaces') = ?",
                                           (replaces,)).fetchone():
                    raise ReviewConflict('This decision has already been corrected')
                if replaces is not None and not db.execute('SELECT 1 FROM entries WHERE id = ?', (replaces,)).fetchone():
                    raise ValueError('Unknown review decision to replace')
                entry = dict(previous=previous, request_id=request_id, replaces=replaces, recorded_at=datetime.now(timezone.utc).isoformat(),
                             decision=decision)
                identifier = digest([bundle_id, entry])
                db.execute('INSERT INTO entries VALUES (?, ?)', (identifier, json.dumps(entry, ensure_ascii=False)))
                db.execute('UPDATE history SET last_entry = ?', (identifier,))
            if new:
                target.replace(path)
        finally:
            if new:
                target.unlink(missing_ok=True)
    return identifier


def bind(workspace, bundle_id, checksum):
    if not (workspace / NAME).exists():
        return
    with _connect(workspace / NAME) as db, db:
        stored = db.execute('SELECT base_sha256 FROM history').fetchone()
        if not stored or stored[0] not in (None, checksum):
            raise ValueError('Review history is bound to another contribution')
        _edition(db, bundle_id, stored[0])
        db.execute('UPDATE history SET base_sha256 = ?', (checksum,))


def archive(workspace, output, manifest):
    if not (workspace / NAME).exists():
        return
    checksum = (workspace / compact_bundle.CHECKSUM).read_text().strip() if compact_bundle.exists(workspace) else None
    with _connect(workspace / NAME, mode='ro') as db:
        record = _edition(db, manifest['id'], checksum)
    output.write(workspace / NAME, NAME)
    output.writestr('review-decisions.json', json.dumps(record, ensure_ascii=False))
