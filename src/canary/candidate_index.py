"""Per-question metadata index. Full records are decoded only for selected IDs."""
from __future__ import annotations

from contextlib import closing
import fcntl
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal, TypedDict
import uuid

NAME = '.candidate-index.sqlite'
SOURCE_ID = re.compile(r'(?:W[1-9][0-9]{0,19}|L[0-9a-f]{24})\Z')


class CandidateMetadata(TypedDict):
    storage: Literal['sqlite']
    records: int
    acquisition_lookup: Literal['selected_ids']


def abstract_text(index):
    positions = {number: word for word, numbers in (index or {}).items() for number in numbers}
    return ' '.join(positions[number] for number in sorted(positions))


def candidate(work):
    return {'id': work['id'].rsplit('/', 1)[-1], 'title': work.get('display_name') or work.get('title'),
        'year': work.get('publication_year'), 'doi': work.get('doi'),
        'abstract': work.get('abstract') if 'local_text' in work else abstract_text(work.get('abstract_inverted_index')),
        **({'local_text': work['local_text'], 'discovery_excerpt': work.get('discovery_excerpt')} if 'local_text' in work else {}),
        'oa_indicated': (work.get('open_access') or {}).get('is_oa'),
        'query_ids': work['query_ids'], 'rank': work['fresh_search_rank']}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def legacy_records(path):
    """Stream the old JSON array one record at a time, including UTF-8 boundaries."""
    decoder = json.JSONDecoder()
    with Path(path).open(encoding='utf-8') as stream:
        buffer = ''
        def available():
            nonlocal buffer
            buffer = buffer.lstrip()
            while not buffer:
                buffer = stream.read(65536).lstrip()
                if not buffer:
                    return False
            return True
        if not available() or buffer[0] != '[':
            raise ValueError('Legacy candidate metadata must be a JSON array')
        buffer = buffer[1:]
        first = True
        while True:
            if not available():
                raise ValueError('Incomplete legacy candidate metadata')
            if buffer[0] == ']':
                buffer = buffer[1:]
                break
            if not first:
                if buffer[0] != ',':
                    raise ValueError('Invalid legacy candidate separator')
                buffer = buffer[1:]
                if not available() or buffer[0] == ']':
                    raise ValueError('Missing legacy candidate record')
            while True:
                try:
                    record, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    chunk = stream.read(65536)
                    if not chunk:
                        raise ValueError('Incomplete legacy candidate record') from None
                    buffer += chunk
            if not isinstance(record, dict):
                raise ValueError('Candidate records must be objects')
            yield record
            buffer, first = buffer[end:], False
        if buffer.strip() or any(chunk.strip() for chunk in iter(lambda: stream.read(65536), '')):
            raise ValueError('Unexpected data after legacy candidates')


def _identifier(work):
    identifier = str(work.get('id', '')).rsplit('/', 1)[-1]
    if not SOURCE_ID.fullmatch(identifier):
        raise ValueError('Invalid candidate source ID')
    return identifier


def _save(db, work, position):
    db.execute('INSERT INTO works VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
               'record=excluded.record,candidate=excluded.candidate',
               (_identifier(work), position, _json(work), json.dumps(candidate(work), ensure_ascii=False)))


class CandidateIndex:
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path = self.workspace / NAME
        # Readers can arrive while discovery is importing a historical workspace.
        # Publish only a complete migration; never modify the old metadata file.
        with (self.workspace / '.candidate-index.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.path.exists():
                temporary = self.workspace / ('.candidate-index-' + uuid.uuid4().hex + '.tmp')
                try:
                    with closing(sqlite3.connect(temporary)) as db, db:
                        db.execute('CREATE TABLE works(id TEXT PRIMARY KEY, position INTEGER UNIQUE NOT NULL, '
                                   'record TEXT NOT NULL, candidate TEXT NOT NULL)')
                        legacy = self.workspace / 'works.json'
                        if legacy.exists():
                            position = 0
                            for work in legacy_records(legacy):
                                old = db.execute('SELECT position FROM works WHERE id=?', (_identifier(work),)).fetchone()
                                if not old:
                                    position += 1
                                rank = old[0] if old else position
                                work.setdefault('query_ids', [])
                                work.setdefault('fresh_search_rank', rank)
                                _save(db, work, rank)
                    temporary.replace(self.path)
                finally:
                    temporary.unlink(missing_ok=True)
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.db = sqlite3.connect(self.path, timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def count(self):
        return self.db.execute('SELECT count(*) FROM works').fetchone()[0]

    def metadata(self) -> CandidateMetadata:
        return {'storage': 'sqlite', 'records': self.count(), 'acquisition_lookup': 'selected_ids'}

    def add_page(self, records, query_id):
        processed, position = 0, self.count()
        with self.db:
            for work in records:
                identifier = _identifier(work)
                old = self.db.execute('SELECT position,record FROM works WHERE id=?', (identifier,)).fetchone()
                if old:
                    saved = json.loads(old[1])
                    if query_id not in saved['query_ids']:
                        saved['query_ids'].append(query_id)
                        _save(self.db, saved, old[0])
                else:
                    position += 1
                    _save(self.db, dict(work, query_ids=[query_id], fresh_search_rank=position), position)
                processed += 1
        return processed

    def selected(self, identifiers, *, projection=False):
        identifiers = list(dict.fromkeys(identifiers))
        field = 'candidate' if projection else 'record'
        result = {}
        for start in range(0, len(identifiers), 500):
            batch = identifiers[start:start + 500]
            for identifier, value in self.db.execute(
                    f'SELECT id,{field} FROM works WHERE id IN ({",".join("?" for _ in batch)})', batch):
                result[identifier] = json.loads(value)
        return result

    def records(self):
        for (value,) in self.db.execute('SELECT record FROM works ORDER BY position'):
            yield json.loads(value)

    def export(self):
        """Stream the existing agent-facing snapshots without building a corpus list."""
        paths = [self.workspace / name for name in ('candidates.jsonl', 'titles.tsv')]
        temporary = [path.with_name(path.name + '.tmp') for path in paths]
        with temporary[0].open('w', encoding='utf-8') as candidates, temporary[1].open('w', encoding='utf-8') as titles:
            for (value,) in self.db.execute('SELECT candidate FROM works ORDER BY position'):
                candidates.write(value + '\n')
                row = json.loads(value)
                titles.write(f'{row["id"]}\t{row["year"]}\t{row["title"]}\n')
        for source, target in zip(temporary, paths):
            source.replace(target)
