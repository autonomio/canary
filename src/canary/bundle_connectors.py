"""Consumer adapters over saved bundles. No research or destination mutation."""
from __future__ import annotations

import json
from pathlib import Path
from functools import lru_cache

from . import research_bundle
from .claim_freshness import unknown as unknown_freshness

RESOLVENT_REVISION = '4b5a056b7686bcf1031fa48a3bde14dd58873834'


def resolvent(workspace):
    workspace = Path(workspace)
    bundle = research_bundle.read(workspace)
    if bundle['state'] != 'finished':
        raise ValueError('Resolvent export requires a finished versioned bundle')
    inventory = {a['path']: a for a in bundle['artifacts']}
    payload_path = workspace / 'payload.json'
    if 'payload.json' in inventory:
        if not payload_path.is_file():
            raise ValueError('Recorded research payload is missing: payload.json')
        payload = json.loads(research_bundle.verified_artifact(workspace, inventory['payload.json']).read_text(encoding='utf-8'))
    elif payload_path.exists():
        raise ValueError('Research payload is not included in the sealed inventory')
    else:
        payload = {}
    refs = {r['id']: r for r in payload.get('references', [])}
    claims, evidence, sources, source_map, evidence_map = [], [], [], {}, {}
    originals = {a['path']: a['sha256'] for a in bundle['artifacts']}
    id_map = {'claims': {}, 'evidence': {}, 'passages': {}}
    @lru_cache(maxsize=8)
    def source_text(name):
        return research_bundle.verified_artifact(workspace, inventory[name]).read_text(encoding='utf-8')
    for index, item in enumerate(payload.get('evidence', []), 1):
        identifier = item['id']
        if identifier in evidence_map:
            raise ValueError('Duplicate evidence ID in bundle')
        local_id, links = 'e' + str(index), []
        for passage in item.get('verbatims', []):
            pid, text = passage['id'], passage.get('text')
            reference = refs.get(passage.get('reference_id'))
            if not text or not reference:
                raise ValueError('Evidence has no reconstructible source passage: ' + identifier)
            canonical = source_text(reference['text_file'])
            start, end = passage.get('start'), passage.get('end')
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(canonical)
                    or canonical[start:end] != text):
                raise ValueError('Verbatim slice mismatch: ' + pid)
            if (reference.get('body_sha256') != originals.get(reference.get('file')) or
                    reference.get('text_sha256') != originals.get(reference.get('text_file'))):
                raise ValueError('Source hash mismatch: ' + reference['id'])
            if pid in source_map:
                existing = sources[int(source_map[pid][1:]) - 1]
                prior = json.loads(existing['locator'])
                if existing['excerpt'] != text or prior['reference_id'] != reference['id'] or prior['start'] != start or prior['end'] != end:
                    raise ValueError('Conflicting canonical passage ID: ' + pid)
            if pid not in source_map:
                sid = 's' + str(len(sources) + 1)
                source_map[pid] = sid
                id_map['passages'][pid] = sid
                locator = {'bundle_id': bundle['id'], 'reference_id': reference['id'],
                    'text_file': reference.get('text_file'), 'start': passage.get('start'), 'end': passage.get('end'),
                    'page': passage.get('page'), 'original_file': reference.get('file'),
                    'original_sha256': originals.get(reference.get('file')), 'text_sha256': originals.get(reference.get('text_file'))}
                sources.append({'id': sid, 'kind': 'publication', 'title': reference.get('title') or reference['id'],
                    'uri': reference.get('url') or reference.get('doi'), 'date': str(reference['year']) if reference.get('year') else None,
                    'locator': json.dumps(locator, sort_keys=True), 'excerpt': text, 'access': 'inspected'})
            links.append(source_map[pid])
        if not links:
            raise ValueError('Evidence lacks verbatim: ' + identifier)
        evidence_map[identifier] = local_id
        id_map['evidence'][identifier] = local_id
        evidence.append({'id': local_id, 'kind': 'external', 'summary': item.get('interpretation') or identifier,
            'source_ids': list(dict.fromkeys(links)), 'independent': False,
            'limitations': ['Source independence is not established by this adapter.',
                           'Recorded evidence relation: ' + item.get('relation', 'unknown')], 'premise_claim_ids': []})
    uncertainty = {'low': 'low', 'moderate': 'medium', 'high': 'high'}
    for index, item in enumerate(payload.get('claims', []), 1):
        identifier = item['id']
        if identifier in id_map['claims']:
            raise ValueError('Duplicate claim ID in bundle')
        if not isinstance(item.get('text'), str) or not item['text'].strip():
            raise ValueError('Claim has no statement: ' + identifier)
        links = item.get('evidence_ids', [])
        if not links or any(x not in evidence_map for x in links):
            raise ValueError('Claim lacks resolvable evidence: ' + identifier)
        level = (item.get('uncertainty') or {}).get('class')
        if level not in uncertainty:
            raise ValueError('Claim lacks recorded uncertainty: ' + identifier)
        local_id = 'c' + str(index)
        id_map['claims'][identifier] = local_id
        claims.append({'id': local_id, 'class': 'derived_inference', 'statement': item['text'],
                       'uncertainty': uncertainty[level], 'evidence_ids': [evidence_map[x] for x in links]})
    summary = '\n\n'.join(x['text'] for x in payload.get('executive_summary', []) if x.get('text'))
    scope = bundle['specification']['resolved']['question']
    limitations = list(dict.fromkeys([*bundle['limitations'], *payload.get('caveats', []),
        'Candidate claim classes require Resolvent admission; this adapter assigns derived_inference without promoting canonical claims.',
        'Freshness, gaps, residue, evidence relations and audit scope are retained in the full research artifact and metadata.',
        'Preserve the accompanying neutral bundle archive for original materials; public source URLs alone do not preserve its bytes.']))
    return {'schema_version': '1.0', 'id': 'RUN-' + bundle['id'].removeprefix('RB-'),
        'input_method': 'source_material', 'created_at': bundle['created_at'], 'researcher': 'Canary',
        'title': scope[:200], 'scope': scope, 'origin': {'mode': 'retrospective', 'coverage': 'partial',
            'limitations': ['A selected research corpus is not exhaustive world knowledge; the independent audit defines checked scope.']},
        'parent_run_id': None, 'brief': None,
        'result': {'summary': summary or 'Saved Canary research bundle; inspect findings and recorded completion limits.',
            'acceptance_outcomes': [], 'limitations': limitations,
            'artifacts': [{'name': 'canary-bundle.json', 'content': json.dumps(bundle, ensure_ascii=False)},
                          {'name': 'canary-research.json', 'content': json.dumps(payload, ensure_ascii=False)}]},
        'sources': sources, 'evidence': evidence, 'claims': claims, 'connectors': [], 'questions': [],
        'handoff': {'summary': 'Normalize this source contribution through Resolvent admission. Preserve provenance and retained residue.',
            'no_claims_reason': None if claims else 'No finalized claims were present in the retained research payload.',
            'suggested_follow_up': list(dict.fromkeys(q['question'] for q in payload.get('next_questions', []) if q.get('question')))},
        'method_metadata': {'producer': 'canary', 'bundle_id': bundle['id'], 'bundle_version': bundle['version'],
            'spec_sha256': bundle['specification']['spec_sha256'], 'connector': 'resolvent', 'connector_version': 1,
            'destination_contract_revision': RESOLVENT_REVISION, 'id_map': id_map,
            'claim_metadata': {id_map['claims'][c['id']]: {'original_kind': c.get('kind'),
                'uncertainty': c.get('uncertainty'), 'freshness': c.get('freshness') or unknown_freshness()} for c in payload.get('claims', [])},
            'evidence_relations': {evidence_map[e['id']]: e.get('relation') for e in payload.get('evidence', [])},
            'independent_audit': payload.get('independent_audit'), 'gaps': payload.get('gaps', []), 'residue': payload.get('residue', [])}}


EXPORTERS = {'resolvent': resolvent}


def export(workspace, connector):
    if connector not in EXPORTERS:
        raise ValueError('Unsupported connector: ' + connector)
    return EXPORTERS[connector](workspace)
