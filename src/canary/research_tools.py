"""Run-scoped scientific tools. Only the API resolves workspaces and providers."""
import json
import re
from pathlib import Path
from typing import TypedDict

from . import coverage_runtime as engine
from . import runs
from .candidate_index import CandidateIndex, WORK_ID
from .citation_selection import rank
from .coverage_protocol import agent_reference


class ToolRequest(TypedDict, total=False):
    plan: dict
    pages: int
    ids: list[str]
    anchors: list[str]
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
          'finalize': {'investigation'}, 'citation-leads': {'anchors'}}


def inline_source_read(action, body):
    """Ordinary text/alternation queries cannot trigger regex backtracking.

    Routing only: other expressions retain the existing cancellable worker.
    The same execute/source functions still validate and answer every query.
    """
    if action != 'source' or not isinstance(body, dict):
        return False
    match = body.get('match')
    return match is None or (isinstance(match, str) and len(match) <= 512
                             and re.fullmatch(r'[\w\s|.,:%/=<>-]*', match) is not None)


def _citation_pass(workspace, anchors=None):
    path = workspace/'citation-leads.json'
    with engine.workspace_lock(workspace, 'discovery'):
        selection = engine.read(workspace/'selection.json', {})
        decisions = selection.get('decisions') if isinstance(selection, dict) else None
        rows = decisions if isinstance(decisions, list) else []
        screened = {d['work_id']: d['decision'] for d in rows
                    if isinstance(d, dict) and isinstance(d.get('work_id'), str)
                    and d.get('decision') in ('include', 'exclude', 'defer')}
        recorded = engine.read(path)
        if recorded is not None:
            if anchors is not None and anchors != recorded['anchors']:
                raise ValueError('This run has already ranked its citation leads.')
            return recorded, screened
        if anchors is not None and any(screened.get(anchor) != 'include' for anchor in anchors):
            raise ValueError('Record an include decision for each citation anchor before ranking.')
        with CandidateIndex(workspace) as index:
            if anchors is None:
                included = [work for work, decision in screened.items()
                            if decision == 'include' and WORK_ID.fullmatch(work)]
                available = index.selected(included, projection=True)
                anchors = [work for work in included if work in available][:3]
                if not anchors:
                    return None, screened
            result = rank(index.citation_records(), anchors, set(screened))
        engine.write(path, result)
        return result, screened


def execute(row, action: str, body: ToolRequest, root: Path):
    if action not in FIELDS or not isinstance(body, dict) or set(body) - FIELDS[action]:
        raise ValueError('Unknown tool or tool fields')
    workspace = Path(row['bundle'])
    for field in ('pages', 'workers', 'context', 'limit', 'page'):
        if field in body and body[field] is not None and (type(body[field]) is not int or body[field] < (0 if field == 'context' else 1)):
            raise ValueError(field + ' is outside its integer range')
    from . import compact_bundle
    if compact_bundle.exists(workspace):
        contribution = compact_bundle.read(workspace, expanded=True)
        if action == 'catalog':
            return [dict(agent_reference(r), source_bytes_retained=False) for r in compact_bundle.payload(contribution).get('references', [])]
        if action == 'source':
            import re
            if not isinstance(body.get('reference_id'), str) or not engine.SOURCE_ID.fullmatch(body['reference_id']):
                raise ValueError('reference_id is required')
            pattern = re.compile(body['match'], re.I) if body.get('match') else None
            graph = contribution['method_metadata']['canary_view']['answer']
            return [dict(p, reference_id=p['source_id'], source_bytes_retained=False) for p in graph.get('passages', [])
                    if p.get('source_id') == body.get('reference_id')
                    and (body.get('page') is None or p.get('page') == body['page'])
                    and (pattern is None or pattern.search(p.get('text', '')))][:body.get('limit', 8)]
        raise ValueError('Research is complete; working sources have been released')
    writes = ('discover', 'acquire', 'finalize', 'citation-leads')
    if action in writes and row.get('status') not in runs._ACTIVE_STATUS:
        raise ValueError('This run is stopped; committed sources remain readable')
    if action in writes and row.get('status') == 'cancelling':
        raise ValueError('This run is stopping; committed sources remain readable')
    if action == 'discover':
        if not isinstance(body.get('plan'), dict):
            raise ValueError('Discovery requires a question plan')
        return engine.discover(workspace, body['plan'], root=root, pages=body.get('pages', 5))
    if action == 'citation-leads':
        if not engine.read(workspace/'run.json', {}).get('citation_graph_requested'):
            raise ValueError('Enable citation_graph selection in the research declaration.')
        anchors = body.get('anchors')
        if (not isinstance(anchors, list) or not 1 <= len(anchors) <= 3 or
                not all(isinstance(a, str) and WORK_ID.fullmatch(a) for a in anchors) or len(set(anchors)) != len(anchors)):
            raise ValueError('Supply one to three distinct OpenAlex work IDs.')
        return _citation_pass(workspace, anchors)[0]
    if action == 'acquire':
        ids = body.get('ids')
        if not isinstance(ids, list) or not ids or not all(isinstance(v, str) and engine.SOURCE_ID.fullmatch(v) for v in ids):
            raise ValueError('ids must be a nonempty list of source IDs')
        if type(body.get('retry_failed', False)) is not bool:
            raise ValueError('retry_failed must be boolean')
        if engine.read(workspace/'run.json', {}).get('citation_graph_requested'):
            report, screened = _citation_pass(workspace)
            if report is None and any(WORK_ID.fullmatch(work) for work in ids):
                raise ValueError('Record an include decision for a discovered OpenAlex anchor before acquisition.')
            leads = [lead['work_id'] for lead in (report or {}).get('leads', [])]
            pending = [work for work in leads if work not in screened]
            if pending:
                raise ValueError('Screen the recorded citation leads before acquisition: ' + ', '.join(pending))
            if any(work in ids and screened[work] != 'include' for work in leads):
                raise ValueError('Acquisition requires an include decision for each requested citation lead.')
        receipts = engine.acquire(workspace, ids, workers=body.get('workers', 12), retry_failed=body.get('retry_failed', False), root=root)
        receipts = [dict(r, reference=agent_reference(r['reference'])) if r.get('reference') else r for r in receipts]
        return {'attempted': len(receipts), 'sources_available': len(engine.catalog(workspace)), 'receipts': receipts}
    if action == 'catalog':
        return [agent_reference(r) for r in engine.catalog(workspace)]
    if action == 'source':
        if not isinstance(body.get('reference_id'), str):
            raise ValueError('reference_id is required')
        return engine.source(workspace, body['reference_id'], match=body.get('match'), page=body.get('page'),
                             context=body.get('context', 2), limit=body.get('limit', 8),
                             record_read=row.get('status') in runs._ACTIVE_STATUS and row.get('status') != 'cancelling')
    if 'investigation' in body and not isinstance(body['investigation'], dict):
        raise ValueError('investigation must contain the structured research output')
    from .source_adapters import validate_record
    for reference in engine.catalog(workspace):
        validate_record(reference)
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
