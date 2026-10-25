"""Run-scoped scientific tools. Only the API resolves workspaces and providers."""
import json
from pathlib import Path
from typing import TypedDict

from . import coverage_runtime as engine
from . import runs


class ToolRequest(TypedDict, total=False):
    plan: dict
    pages: int
    ids: list[str]
    workers: int
    retry_failed: bool
    reference_id: str
    match: str | None
    page: int | None
    context: int
    limit: int
    investigation: dict


FIELDS = {'discover': {'plan', 'pages'}, 'acquire': {'ids', 'workers', 'retry_failed'},
          'catalog': set(), 'source': {'reference_id', 'match', 'page', 'context', 'limit'},
          'finalize': {'investigation'}}


def execute(row, action: str, body: ToolRequest, root: Path):
    if action not in FIELDS or not isinstance(body, dict) or set(body) - FIELDS[action]:
        raise ValueError('Unknown tool or tool fields')
    workspace = Path(row['bundle'])
    if action in ('discover', 'acquire', 'finalize') and row.get('status') not in runs._ACTIVE_STATUS:
        raise ValueError('This run is stopped; committed sources remain readable')
    if action in ('discover', 'acquire', 'finalize') and row.get('status') == 'cancelling':
        raise ValueError('This run is stopping; committed sources remain readable')
    for field in ('pages', 'workers', 'context', 'limit', 'page'):
        if field in body and body[field] is not None and (type(body[field]) is not int or body[field] < (0 if field == 'context' else 1)):
            raise ValueError(field + ' is outside its integer range')
    if action == 'discover':
        engine.validate_plan(body.get('plan'))
        path = workspace/'question-plan-submitted.json'
        engine.write(path, body['plan'])
        return engine.discover(workspace, path, root=root, pages=body.get('pages', 5))
    if action == 'acquire':
        ids = body.get('ids')
        if not isinstance(ids, list) or not ids or not all(isinstance(v, str) and engine.WORK_ID.fullmatch(v) for v in ids):
            raise ValueError('ids must be a nonempty list of OpenAlex work IDs')
        if type(body.get('retry_failed', False)) is not bool:
            raise ValueError('retry_failed must be boolean')
        receipts = engine.acquire(workspace, ids, workers=body.get('workers', 12), retry_failed=body.get('retry_failed', False))
        return {'attempted': len(receipts), 'sources_available': len(engine.catalog(workspace)), 'receipts': receipts}
    if action == 'catalog':
        return engine.catalog(workspace)
    if action == 'source':
        if not isinstance(body.get('reference_id'), str):
            raise ValueError('reference_id is required')
        return engine.source(workspace, body['reference_id'], match=body.get('match'), page=body.get('page'),
                             context=body.get('context', 2), limit=body.get('limit', 8),
                             record_read=row.get('status') in runs._ACTIVE_STATUS and row.get('status') != 'cancelling')
    if 'investigation' in body and not isinstance(body['investigation'], dict):
        raise ValueError('investigation must contain the structured research output')
    if 'investigation' in body: engine.write(workspace/'investigation.json', body['investigation'])
    payload = engine.bind_payload(workspace)
    return {'accounting': payload['accounting'], 'mechanical_audit': payload['mechanical_audit']}


if __name__ == '__main__':
    import sys
    packet = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    target = Path(sys.argv[2])
    try:
        result = execute(packet['row'], packet['action'], packet['body'], Path(packet['root']))
        engine.write(target, {'result':result})
    except Exception as error:
        engine.write(target, {'error':str(error)})
        raise
