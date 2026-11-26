"""Per-query search receipts, grouped by discovery round."""
import json
from itertools import groupby


def start_round(workspace, page_limit):
    from .coverage_runtime import read, write
    path = workspace / 'search-round.json'
    number = read(path, {}).get('round', 0) + 1
    write(path, dict(round=number, page_limit=page_limit))
    return number


def searches(workspace, progress):
    receipts = []
    for directory in (workspace, workspace / 'common', workspace / 'research'):
        path = directory / 'openalex-requests.json'
        if path.is_file() and path.resolve().is_relative_to(workspace.resolve()):
            receipts.extend(json.loads(path.read_text()))
            break
    for name in ('additional-source-requests.jsonl', 'local-search-requests.jsonl'):
        for directory in (workspace, workspace / 'common', workspace / 'research'):
            path = directory / name
            if path.is_file() and path.resolve().is_relative_to(workspace.resolve()):
                receipts.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
                break
    keys = ('round', 'page', 'requested_at', 'status', 'matched', 'retrieved', 'ended', 'page_limit')
    rows = []
    for query_id, state in progress.get('queries', {}).items():
        requests = [r for r in receipts if r.get('query_id') == query_id]
        groups = [(number, list(items)) for number, items in groupby(requests, key=lambda r: r.get('round'))]
        for index, (number, group) in enumerate(groups or [(None, [])]):
            last = group[-1] if group else {}
            latest = index + 1 >= len(groups)
            matched = next((r['matched'] for r in reversed(group) if r.get('matched') is not None), None)
            if latest and matched is None:
                matched = state.get('matched')
            counts = [r.get('retrieved') for r in group if r.get('status') == 200]
            retrieved = sum(counts) if counts and all(type(n) is int for n in counts) else None
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
            rows.append(dict(query_id=query_id, source=state.get('source') or progress.get('adapter') or 'openalex',
                             search=state.get('search') or 'Search terms were not recorded.', attempted=bool(group or state.get('pages') or state.get('failure_status')
                                                                   or state.get('failure') or state.get('complete')),
                             round=number, matched=matched, retrieved=retrieved, ended=ended,
                             last_search=next((r['requested_at'] for r in reversed(group) if r.get('requested_at')), None),
                             requests=[{k: r[k] for k in keys if k in r} for r in group]))
    return rows
