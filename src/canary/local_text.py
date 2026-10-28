"""API-owned adapter for an explicitly allowed directory of UTF-8 paper texts.

The disposable FTS5 index is a retrieval view. Selected bytes and exact passages
use the ordinary research source pipeline; local text never certifies a paper.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from .research_spec import digest

INDEX = '.local-text.sqlite'
SCOPE = ('Search covers only the indexed local UTF-8 texts. Text identity, completeness, '
         'tables, figures and publication versions require researcher verification. '
         'Only selected source bytes are retained in the bundle; the derived search index is excluded.')


def _allowed(path):
    try:
        path = Path(path).resolve(strict=True)
        roots = [Path(value).expanduser().resolve(strict=True)
                 for value in os.environ.get('CANARY_LOCAL_TEXT_ROOTS', '').split(os.pathsep) if value]
    except OSError:
        raise ValueError('Local text path or configured source root does not exist') from None
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError('Local text path is outside CANARY_LOCAL_TEXT_ROOTS; configure the service with an explicit source root')
    return path


def validate_access(config):
    directory = _allowed(config['directory'])
    if not directory.is_dir():
        raise ValueError('Local text directory does not exist')
    if config.get('metadata'):
        metadata = _allowed(config['metadata'])
        if not metadata.is_file() or metadata.suffix.lower() != '.jsonl':
            raise ValueError('Local metadata must be a regular JSONL file')
    return directory


def _bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Local source must be a regular file')
        return stream.read()


def _metadata(config):
    """Explicit relative filenames, or DOI/OpenAlex slugs used by saved libraries."""
    if not config.get('metadata'):
        return {}, None
    raw = _bytes(_allowed(config['metadata']))
    records = {}
    for number, line in enumerate(raw.decode('utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise ValueError(f'Invalid metadata JSON on line {number}') from None
        if not isinstance(row, dict):
            raise ValueError(f'Metadata line {number} must be an object')
        if row.get('doi') is not None and not isinstance(row['doi'], str):
            raise ValueError(f'Metadata DOI on line {number} must be text')
        if row.get('openalex_id') is not None and not isinstance(row['openalex_id'], str):
            raise ValueError(f'Metadata OpenAlex ID on line {number} must be text')
        filename = row.get('file')
        if filename is None:
            doi = row.get('doi') or ''
            doi = doi.removeprefix('https://doi.org/').removeprefix('http://doi.org/')
            identity = doi or (row.get('openalex_id') or '').replace('https://openalex.org/', 'oa-')
            if not identity:
                continue
            filename = ''.join(c if c.isalnum() else '_' for c in identity)[:120] + '.txt'
        if not isinstance(filename, str) or Path(filename).is_absolute() or '..' in Path(filename).parts:
            raise ValueError(f'Metadata line {number} must identify a relative source file')
        fields = {key: row.get(key) for key in ('title', 'doi', 'year', 'authors', 'abstract', 'openalex_id')}
        for key in ('title', 'doi', 'abstract', 'openalex_id'):
            if fields[key] is not None and not isinstance(fields[key], str):
                raise ValueError(f'Metadata {key} on line {number} must be text')
        if fields['year'] is not None and type(fields['year']) is not int:
            raise ValueError(f'Metadata year on line {number} must be an integer')
        authors = fields['authors'] or []
        if not isinstance(authors, list) or any(not isinstance(a, str) for a in authors):
            raise ValueError(f'Metadata authors on line {number} must be a list of names')
        fields['authors'] = authors
        if filename in records and records[filename] != fields:
            records[filename] = None  # A filename collision cannot establish identity.
        else:
            records[filename] = fields
    return records, hashlib.sha256(raw).hexdigest()


def prepare(workspace, config):
    from .coverage_runtime import workspace_lock
    with workspace_lock(Path(workspace), 'local-text-index'):
        return _prepare(workspace, config)


def _prepare(workspace, config):
    """Hash and index once within a fresh run; never edit the source directory."""
    from .coverage_runtime import event, now, read, write
    workspace = Path(workspace)
    directory = validate_access(config)
    manifest_path = workspace / 'local-corpus.json'
    if manifest_path.is_file() and (workspace / INDEX).is_file():
        manifest = read(manifest_path)
        if manifest['configuration'] != config:
            raise ValueError('Local corpus configuration changed within this run')
        return manifest
    metadata, metadata_sha = _metadata(config)
    started = time.monotonic()
    inventory, excluded, total = [], [], 0
    temporary = workspace / (INDEX + '.tmp')
    temporary.unlink(missing_ok=True)
    try:
        with sqlite3.connect(temporary) as db:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('CREATE TABLE documents (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            db.execute("CREATE VIRTUAL TABLE search USING fts5(title, abstract, body, content='')")
            for base, dirs, names in os.walk(directory, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(base) / d).is_symlink())
                for name in sorted(names):
                    if name.startswith('.') or Path(name).suffix.lower() != '.txt':
                        continue
                    path = Path(base) / name
                    relative = path.relative_to(directory).as_posix()
                    try:
                        if path.is_symlink() or not path.resolve().is_relative_to(directory):
                            raise ValueError('Symlink or path outside source root')
                        raw = _bytes(path)
                        text = raw.decode('utf-8')
                        if not text.strip():
                            raise ValueError('Empty source text')
                    except (OSError, UnicodeError, ValueError) as error:
                        excluded.append({'file': relative, 'reason': type(error).__name__ + ': ' + str(error)})
                        continue
                    identifier = 'L' + hashlib.sha256((str(directory) + '/' + relative).encode()).hexdigest()[:24]
                    source = {'file': relative, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
                    fields = metadata.get(relative) or {}
                    title = fields.get('title') or path.stem
                    record = {'id': identifier, 'display_name': title, 'doi': fields.get('doi'),
                        'publication_year': fields.get('year'), 'abstract': fields.get('abstract'),
                        'authorships': [{'author': {'display_name': a}} for a in fields.get('authors', [])],
                        'openalex_id': fields.get('openalex_id'), 'local_text': source,
                        'discovery_excerpt': text[:1500]}
                    rowid = db.execute('INSERT INTO documents VALUES (?, ?)',
                                       (identifier, json.dumps(record, ensure_ascii=False))).lastrowid
                    db.execute('INSERT INTO search(rowid, title, abstract, body) VALUES (?, ?, ?, ?)',
                               (rowid, title, fields.get('abstract') or '', text))
                    inventory.append({'id': identifier, **source})
                    total += len(raw)
                    if len(inventory) % 100 == 0:
                        write(workspace / 'local-corpus-progress.json', {'status': 'indexing',
                              'indexed_files': len(inventory), 'excluded_files': len(excluded), 'bytes': total})
                        marker = os.environ.get('CANARY_CANCEL_MARKER')
                        if marker and Path(marker).exists():
                            raise RuntimeError('Local text indexing cancelled')
            if not inventory:
                raise ValueError('No readable nonempty UTF-8 .txt sources in the allowed directory')
            db.execute("INSERT INTO search(search) VALUES ('optimize')")
        manifest = {'adapter': 'local_text', 'version': 1, 'configuration': config,
            'created_at': now(), 'indexed_files': len(inventory), 'bytes': total,
            'metadata_sha256': metadata_sha, 'metadata_records': len(metadata),
            'matched_metadata_files': sum(bool(metadata.get(item['file'])) for item in inventory),
            'ambiguous_metadata_files': [item['file'] for item in inventory if item['file'] in metadata and metadata[item['file']] is None],
            'files': inventory, 'excluded_files': excluded, 'scope_note': SCOPE,
            'search_method': 'SQLite FTS5 lexical search; BM25 ranking; no semantic expansion',
            'seconds': round(time.monotonic() - started, 3)}
        manifest['snapshot_sha256'] = digest({'files': inventory, 'metadata_sha256': metadata_sha})
        temporary.replace(workspace / INDEX)
        write(manifest_path, manifest)
        write(workspace / 'local-corpus-progress.json', {k: v for k, v in manifest.items() if k not in ('files', 'excluded_files')})
        event(workspace, 'local_corpus_indexed', indexed_files=len(inventory), excluded_files=len(excluded), seconds=manifest['seconds'])
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def discover(workspace, plan, config, pages):
    from .coverage_runtime import _commit_discovery, event, read, write
    from .candidate_index import CandidateIndex
    workspace = Path(workspace)
    manifest = prepare(workspace, config)
    previous = read(workspace / 'question-plan.json', {})
    run_question = read(workspace / 'run.json', {}).get('question')
    if plan['question'] != (run_question or previous.get('question', plan['question'])):
        raise ValueError('QuestionPlan differs from this run question')
    progress = read(workspace / 'search-progress.json', {'queries': {}})
    known = {q['id'] for q in plan['queries']}
    plan['queries'].extend(q for q in previous.get('queries', []) if q['id'] not in known)
    for query in plan['queries']:
        state = progress['queries'].get(query['id'])
        if state and state['search'] != query['search']:
            raise ValueError('Change a query ID when changing its search string')
    progress.update(adapter='local_text', corpus_snapshot_sha256=manifest['snapshot_sha256'],
                    indexed_files=manifest['indexed_files'], scope_note=SCOPE, keyword_requests=0)
    with sqlite3.connect((workspace / INDEX).resolve().as_uri() + '?mode=ro', uri=True) as db, CandidateIndex(workspace) as candidates:
        # Reject invalid syntax before it can poison the persistent query history.
        counts = {}
        for query in plan['queries']:
            try:
                counts[query['id']] = db.execute('SELECT count(*) FROM search WHERE search MATCH ?', (query['search'],)).fetchone()[0]
            except sqlite3.OperationalError as error:
                raise ValueError('Invalid local text query ' + query['id'] + ': ' + str(error)) from None
        write(workspace / 'question-plan.json', plan)
        for query in plan['queries']:
            state = progress['queries'].setdefault(query['id'], {'search': query['search'], 'pages': 0, 'complete': False})
            if state['complete']:
                continue
            offset, limit = state['pages'] * 200, pages * 200
            matched = counts[query['id']]
            rows = db.execute('SELECT record FROM search JOIN documents ON documents.rowid=search.rowid '
                'WHERE search MATCH ? ORDER BY bm25(search, 5.0, 2.0, 1.0), documents.id LIMIT ? OFFSET ?',
                (query['search'], limit, offset))
            returned = candidates.add_page((json.loads(value) for (value,) in rows), query['id'])
            state.update(pages=state['pages'] + max(1, (returned + 199) // 200), matched=matched, returned=min(offset + returned, matched),
                         complete=offset + returned >= matched, truncated=offset + returned < matched)
            state.pop('failure', None)
            _commit_discovery(workspace, candidates, progress, plan)
            event(workspace, 'local_search_page', query_id=query['id'], matched=matched, returned=returned, complete=state['complete'])
    return progress


def fetch(work, config):
    directory = validate_access(config)
    source = work['local_text']
    relative = Path(source['file'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Local source path must stay inside its configured directory')
    path = directory / relative
    if path.is_symlink() or not path.resolve().is_relative_to(directory):
        raise ValueError('Local source path escaped its configured directory')
    raw = _bytes(path)
    if len(raw) != source['bytes'] or hashlib.sha256(raw).hexdigest() != source['sha256']:
        raise ValueError('Local source changed after indexing; start a new run to use changed texts')
    doi = work.get('doi')
    url = doi if doi and doi.startswith(('https://doi.org/', 'http://doi.org/')) else 'https://doi.org/' + doi if doi else None
    return (raw, 'text/plain', url), False
