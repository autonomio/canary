"""Per-query search receipts, grouped by discovery round."""
import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote_plus

from httpx import URL


def request_address(url: URL) -> str:
    """Keep only the public endpoint of a request URL."""
    return str(url.copy_with(username=None, password=None, query=None, fragment=None))


def private_query_key(key: str) -> bool:
    key = key.lower()
    return key.startswith(('x-amz-', 'x-goog-')) or key.replace('-', '').replace('_', '') in {
        'accesstoken', 'token', 'apikey', 'key', 'signature', 'sig', 'expires', 'policy', 'keypairid',
        'awsaccesskeyid', 'googleaccessid', 'password', 'secret', 'credential', 'refreshtoken', 'idtoken',
        'clientsecret', 'securitytoken',
    }


def paper_address(url: URL) -> str:
    """Preserve paper query fields while removing recognised credentials."""
    fields = (field for field in url.query.split(b'&')
              if not private_query_key(unquote_plus(field.partition(b'=')[0].decode('utf-8', errors='replace'))))
    return str(url.copy_with(username=None, password=None, query=b'&'.join(fields) or None, fragment=None))


def start_round(workspace: Path, page_limit: int) -> int:
    """Record the page allowance and return the next discovery round."""
    from .coverage_runtime import read, write
    path = workspace / 'search-round.json'
    number = read(path, {}).get('round', 0) + 1
    write(path, dict(round=number, page_limit=page_limit))
    return number


def working_file(workspace: Path | None, name: str) -> Path | None:
    """Locate a retained working file inside the bundle."""
    if workspace is not None:
        for directory in (workspace, workspace / 'common', workspace / 'research'):
            path = directory / name
            if path.is_file() and path.resolve().is_relative_to(workspace.resolve()):
                return path
    return None


def searches(workspace: Path | None, progress: dict, plan: Iterable[dict] = (), *,
             warnings: list[str] | None = None) -> list[dict]:
    """Reconstruct search rounds and distinguish complete counts from lower bounds."""
    receipts = defaultdict(list)
    incomplete_logs = set()
    sources = {'openalex': 'openalex-requests.json', 'local_text': 'local-search-requests.jsonl'}
    additional = 'additional-source-requests.jsonl'
    for source, name in [*sources.items(), (None, additional)]:
        path = working_file(workspace, name)
        if path is None:
            continue
        incomplete = False
        records = []
        try:
            if path.suffix == '.json':
                saved = json.loads(path.read_bytes())
                if isinstance(saved, list):
                    records = saved
                else:
                    incomplete = True
            else:
                with path.open('rb') as stream:
                    for line in stream:
                        if line.strip():
                            try:
                                records.append(json.loads(line))
                            except ValueError:
                                incomplete = True
        except (OSError, ValueError):
            incomplete = True
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get('query_id'), str):
                incomplete = True
                continue
            receipts[record['query_id']].append((name, source, record))
        if incomplete:
            incomplete_logs.add(name)
        if incomplete and warnings is not None:
            warnings.append(f'Some requests in {name} could not be read. Counts from these receipts may be incomplete.')
    keys = ('round', 'page', 'requested_at', 'status', 'matched', 'retrieved', 'ended', 'page_limit')
    rows = []
    queries = {q['id']: q for q in plan if isinstance(q, dict) and q.get('id')}
    for key, state in (progress.get('queries') or {}).items():
        if isinstance(state, dict):
            queries[key] = {**queries.get(key, {}), **state}
    for query_id in receipts:
        queries.setdefault(query_id, {})
    for query_id, state in queries.items():
        groups = defaultdict(list)
        planned_source = state.get('source') or progress.get('adapter')
        for name, provider, record in receipts[query_id]:
            source = record.get('source') or provider or planned_source or 'Source unknown'
            groups[source, record.get('round'), name].append(record)
        if not groups:
            groups[planned_source or 'openalex', None, None] = []
        providers = {key[0] for key in groups}
        state_source = planned_source or (next(iter(providers)) if len(providers) == 1 else None)
        latest_round = next((key for key in reversed(groups) if key[0] == state_source), None)
        for (source, number, name), group in groups.items():
            query_state = state if source == state_source else {}
            last = group[-1] if group else {}
            terms = next((r['search'] for r in group if r.get('search')), query_state.get('search'))
            latest = (source, number, name) == latest_round
            matched = next((r['matched'] for r in reversed(group) if r.get('matched') is not None), None)
            if matched is None and (not group or (number is None and latest and any(r.get('status') == 200 for r in group))):
                matched = query_state.get('matched')
            counts = [r.get('retrieved') for r in group if r.get('status') == 200]
            known = [n for n in counts if type(n) is int and n >= 0]
            retrieved = sum(known) if known else None
            if group and all(isinstance(r.get('status'), int) and r['status'] >= 400 for r in group):
                retrieved = 0
            if not group and query_state.get('returned') is not None:
                retrieved = query_state['returned']
            ended = last.get('ended')
            if not ended and last.get('status') not in (None, 200):
                ended = f"Request failed (HTTP {last['status']})."
            if not ended and group and last.get('status') is None:
                ended = 'The request did not complete.'
            if not ended and latest:
                if query_state.get('failure_status'):
                    ended = f"Request failed (HTTP {query_state['failure_status']})."
                elif query_state.get('failure'):
                    ended = 'The search was interrupted.'
                elif query_state.get('complete'):
                    ended = 'The source returned its final page.'
                elif query_state.get('truncated'):
                    ended = 'More results remained when fetching stopped.'
            attempted = bool(group or retrieved is not None or query_state.get('pages') or query_state.get('failure_status') or query_state.get('failure') or query_state.get('complete'))
            rows.append(dict(query_id=query_id, source=source,
                             search=terms or 'Search terms were not recorded.', attempted=attempted,
                             round=number, matched=matched, retrieved=retrieved, retrieved_partial=name in incomplete_logs or len(known) < len(counts), ended=ended,
                             last_search=next((r['requested_at'] for r in reversed(group) if r.get('requested_at')), None),
                             requests=[{k: r[k] for k in keys if k in r} for r in group]))
    return rows
