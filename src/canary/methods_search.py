"""Per-query search receipts, grouped by discovery round."""
import json
from itertools import groupby


def request_address(url):
    return str(url.copy_with(username=None, password=None, query=None, fragment=None))


def start_round(workspace, page_limit):
    from .coverage_runtime import read, write
    path = workspace / 'search-round.json'
    number = read(path, {}).get('round', 0) + 1
    write(path, dict(round=number, page_limit=page_limit))
    return number


def working_file(workspace, name):
    if workspace is not None:
        for directory in (workspace, workspace / 'common', workspace / 'research'):
            path = directory / name
            if path.is_file() and path.resolve().is_relative_to(workspace.resolve()):
                return path
    return None


def searches(workspace, progress, plan=(), *, warnings=None):
    receipts = []
    for name in ('openalex-requests.json', 'additional-source-requests.jsonl', 'local-search-requests.jsonl'):
        path = working_file(workspace, name)
        if path is None:
            continue
        incomplete = False
        try:
            if path.suffix == '.json':
                records = json.loads(path.read_bytes())
                if isinstance(records, list):
                    receipts.extend(records)
                else:
                    incomplete = True
            else:
                with path.open('rb') as stream:
                    for line in stream:
                        if line.strip():
                            try:
                                receipts.append(json.loads(line))
                            except ValueError:
                                incomplete = True
        except (OSError, ValueError):
            incomplete = True
        if incomplete and warnings is not None:
            warnings.append(f'Some requests in {name} could not be read. Counts from these receipts may be incomplete.')
    keys = ('round', 'page', 'requested_at', 'status', 'matched', 'retrieved', 'ended', 'page_limit')
    rows = []
    queries = {q['id']: q for q in plan if isinstance(q, dict) and q.get('id')}
    for key, state in (progress.get('queries') or {}).items():
        if isinstance(state, dict):
            queries[key] = {**queries.get(key, {}), **state}
    for query_id, state in queries.items():
        requests = [r for r in receipts if isinstance(r, dict) and r.get('query_id') == query_id]
        groups = [(number, list(items)) for number, items in groupby(requests, key=lambda r: r.get('round'))]
        for index, (number, group) in enumerate(groups or [(None, [])]):
            last = group[-1] if group else {}
            latest = index + 1 >= len(groups)
            matched = next((r['matched'] for r in reversed(group) if r.get('matched') is not None), None)
            if matched is None and (not group or (number is None and latest and any(r.get('status') == 200 for r in group))):
                matched = state.get('matched')
            counts = [r.get('retrieved') for r in group if r.get('status') == 200]
            known = [n for n in counts if type(n) is int and n >= 0]
            retrieved = sum(known) if known else None
            if group and all(isinstance(r.get('status'), int) and r['status'] >= 400 for r in group):
                retrieved = 0
            if not group and state.get('returned') is not None:
                retrieved = state['returned']
            ended = last.get('ended')
            if not ended and last.get('status') not in (None, 200):
                ended = f"Request failed (HTTP {last['status']})."
            if not ended and group and last.get('status') is None:
                ended = 'The request did not complete.'
            if not ended and latest:
                if state.get('failure_status'):
                    ended = f"Request failed (HTTP {state['failure_status']})."
                elif state.get('failure'):
                    ended = 'The search was interrupted.'
                elif state.get('complete'):
                    ended = 'The source returned its final page.'
                elif state.get('truncated'):
                    ended = 'More results remained when fetching stopped.'
            attempted = bool(group or state.get('pages') or state.get('failure_status') or state.get('failure') or state.get('complete'))
            rows.append(dict(query_id=query_id, source=state.get('source') or progress.get('adapter') or 'openalex',
                             search=state.get('search') or 'Search terms were not recorded.', attempted=attempted,
                             round=number, matched=matched, retrieved=retrieved, retrieved_partial=len(known) < len(counts), ended=ended,
                             last_search=next((r['requested_at'] for r in reversed(group) if r.get('requested_at')), None),
                             requests=[{k: r[k] for k in keys if k in r} for r in group]))
    return rows
