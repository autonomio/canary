"""Consumer adapters over saved bundles. No research or destination mutation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from functools import lru_cache
from urllib.parse import urlparse

from . import research_bundle
from .claim_freshness import unknown as unknown_freshness
from .claim_uncertainty import review as uncertainty_review

# The receiving contract: its validator refuses a contribution file above MAX_FILE_BYTES (10 MiB),
# and its research-state reader decodes canary.references version 1.
RESOLVENT_REVISION = '4993e7210429014cf217b0414e260156c95c8263'
CONTRIBUTION_LIMIT_BYTES = 10 * 1024 * 1024
UNCERTAINTY_POLICY = ('Resolvent candidate uncertainty is at least medium because this adapter does not '
                      'establish independent evidence. Original Canary uncertainty remains in claim_metadata.')


class ContributionTooLarge(ValueError):
    """A compact contribution above the destination file limit; nothing is exported."""

    def __init__(self, size):
        self.size, self.limit = size, CONTRIBUTION_LIMIT_BYTES
        super().__init__(f'The Resolvent contribution would be {size:,} bytes; Resolvent refuses files above '
                         f'{self.limit:,} bytes. Nothing was exported; the retained bundle keeps the research.')


def contribution(value):
    """The saved compact file (UTF-8 JSON and one newline) against the destination limit."""
    from .bundle_encoding import dumps
    size = len(dumps(value).encode('utf-8')) + 1
    return {'bytes': size, 'limit_bytes': CONTRIBUTION_LIMIT_BYTES, 'within_limit': size <= CONTRIBUTION_LIMIT_BYTES}


def compact(value, expanded=None):
    """The compact contribution: encoded as recorded, or packed again from `expanded`.

    A contribution recorded before its bundle manifest was compressed is packed again only
    when it is over the limit; its native records stay as recorded.
    """
    from . import bundle_encoding
    encoding = bundle_encoding.header(value)
    if encoding is None:
        return bundle_encoding.encode(value)
    expanded = bundle_encoding.expand(value) if expanded is None else expanded  # Only a reconstructible one.
    if 'compressed' in encoding or contribution(value)['within_limit']:
        return value
    return bundle_encoding.encode(expanded)


def resolvent(workspace):
    workspace = Path(workspace)
    from . import compact_bundle
    if compact_bundle.exists(workspace):
        return compact_bundle.read(workspace)
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
    run = json.loads((workspace/'run.json').read_text()) if (workspace/'run.json').is_file() else {}
    if (run.get('verification_version') and not payload.get('verification', {}).get('version')
            and (payload.get('claims') or payload.get('executive_summary'))):
        raise ValueError('This run has not produced a verified final claim projection; working sources remain available')
    verified = payload.get('verification', {}).get('version') == 1
    if verified:
        from .corrections import verify_final
        verify_final(payload)
    refs = {r['id']: r for r in payload.get('references', [])}
    claims, evidence, sources, source_map, evidence_map = [], [], [], {}, {}
    source_metadata = {}
    paper_metadata = {identifier: ref["paper_metadata"] for identifier, ref in refs.items()
                      if ref.get("paper_metadata") is not None}
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
                if reference['id'] in paper_metadata:
                    locator['paper_metadata_id'] = reference['id']
                if reference.get('source_kind'):
                    source_metadata[sid] = {k: reference[k] for k in ('adapter', 'source_kind', 'source_metadata', 'record_url')}
                    locator.update(source_metadata[sid])
                sources.append({'id': sid, 'kind': 'web' if reference.get('adapter') in ('clinicaltrials', 'osf') else 'publication',
                    'title': reference.get('title') or reference['id'],
                    'uri': reference.get('record_url') or reference.get('url') or reference.get('doi'),
                    'date': reference.get('source_metadata', {}).get('record_date')
                            or paper_metadata.get(reference['id'], {}).get('attributes', {}).get('publication_date')
                            or (str(reference['year']) if reference.get('year') else None),
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
    audit_checks = {}
    audit = payload.get('independent_audit')
    checks = audit.get('claim_checks') if isinstance(audit, dict) else None
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, dict) and isinstance(check.get('claim_id'), str):
            audit_checks.setdefault(check['claim_id'], []).append(check)
    withheld_evidence = {e['id'] for e in payload.get('withheld_evidence', [])}
    def reviewed_uncertainty(claim):
        checks = audit_checks.get(claim['id'], [])
        check = checks[0] if len(checks) == 1 else {}
        # Evidence withheld with other claims remains this run's audited basis for the recommendation.
        reviewed = uncertainty_review(check, state=bundle['specification']['resolved'].get('research_state'),
                                      evidence_ids=evidence_map.keys() | withheld_evidence)
        if len(checks) > 1:
            reviewed['reason'] = 'Multiple recorded audit checks share this claim ID; no single uncertainty recommendation can be assigned.'
            reviewed['limitations'].append('Duplicate audit checks; inspect all rows in independent_audit before destination review.')
        if withheld_evidence & set(reviewed.get('evidence_ids', [])):
            reviewed['limitations'].append('Recommendation also cites evidence withheld from active output; see method_metadata.withheld_evidence.')
        return dict(reviewed, evidence_ids=[evidence_map[e] for e in reviewed.get('evidence_ids', []) if e in evidence_map],
                    original_evidence_ids=reviewed.get('evidence_ids', []), disposition='requires_destination_review')
    uncertainty = {'low': 'medium', 'moderate': 'medium', 'high': 'high'}
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
    limitations.append(UNCERTAINTY_POLICY)
    result = {'schema_version': '1.0', 'id': 'RUN-' + bundle['id'].removeprefix('RB-'),
        'input_method': 'source_material', 'created_at': bundle['created_at'], 'researcher': 'Canary',
        'title': scope[:200], 'scope': scope, 'origin': {'mode': 'retrospective', 'coverage': 'partial',
            'limitations': ['A selected research corpus is not exhaustive world knowledge; the independent audit defines checked scope.']},
        'parent_run_id': None, 'brief': None,
        'result': {'summary': summary or 'Saved Canary research bundle; inspect findings and recorded completion limits.',
            'acceptance_outcomes': [], 'limitations': limitations,
            'artifacts': [{'name': 'canary-bundle.json', 'content': json.dumps(bundle, ensure_ascii=False)},
                          {'name': 'canary-research.json', 'content': json.dumps(payload, ensure_ascii=False)}]},
        'sources': sources, 'evidence': evidence, 'claims': claims, 'connectors': [], 'questions': [],
        'handoff': {'summary': 'Normalize this source contribution through Resolvent admission. Preserve provenance and retained residue. '
                    'Explicitly accept, override or defer each assessed claim_metadata.uncertainty_review with a reason; '
                    'for a matched existing claim, use recorded reassessment. A recommendation is not an applied change.'
                    + (' Source kinds and provider metadata are retained in source locators and method_metadata.source_metadata. '
                       'Distinguish posted clinical results from registry plans; OSF study plans do not establish findings; '
                       'patent disclosures are not independent empirical validation. Related reports may share the same trial or patent family.'
                       if source_metadata else ''),
            'no_claims_reason': None if claims else 'No finalized claims were present in the retained research payload.',
            'suggested_follow_up': list(dict.fromkeys(q['question'] for q in payload.get('next_questions', []) if q.get('question')))},
        'method_metadata': {'producer': 'canary', 'bundle_id': bundle['id'], 'bundle_version': bundle['version'],
            'spec_sha256': bundle['specification']['spec_sha256'], 'connector': 'resolvent', 'connector_version': 3,
            'uncertainty_policy': UNCERTAINTY_POLICY,
            'destination_contract_revision': RESOLVENT_REVISION, 'id_map': id_map,
            'claim_metadata': {id_map['claims'][c['id']]: {'original_kind': c.get('kind'),
                'uncertainty': c.get('uncertainty'), 'uncertainty_review': reviewed_uncertainty(c), 'freshness': c.get('freshness') or unknown_freshness()} for c in payload.get('claims', [])},
            'evidence_relations': {evidence_map[e['id']]: e.get('relation') for e in payload.get('evidence', [])},
            **({'source_metadata': source_metadata} if source_metadata else {}),
            **({'paper_metadata': paper_metadata} if paper_metadata else {}),
            **({'additional_sources': payload['additional_sources']} if payload.get('additional_sources') else {}),
            'independent_audit': payload.get('independent_audit'), 'gaps': payload.get('gaps', []), 'residue': payload.get('residue', [])}}
    return verified_export(result, payload) if verified else result


VERIFIED_UNCERTAINTY_POLICY = ('Original low uncertainty is retained only with explicitly audited independent, inspected, '
    'addressable supporting evidence; otherwise candidate uncertainty is at least medium. '
    'Source independence is not scientific certainty. Original judgments remain in claim_metadata.')


def round_record(record):
    """A round's bound payload repeats the research graph; native metadata names it by digest instead.

    The complete hash-checked record stays in canary-research.json and the revision API.
    """
    if 'payload' not in record:
        return record
    snapshot = json.dumps(record['payload'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return dict({k: v for k, v in record.items() if k != 'payload'},
                payload_sha256=hashlib.sha256(snapshot.encode('utf-8')).hexdigest())


def verified_export(result, payload):
    """Mechanical v5 mapping of the producer's already verified, closed graph."""
    from . import audit_validation
    from .corrections import audit_basis
    metadata = result['method_metadata']
    mapping = metadata['id_map']
    audit = payload['independent_audit']
    current = audit_validation.snapshot(audit_basis(payload)) == audit.get('snapshot_sha256')
    checks = {c['claim_id']: c for c in audit.get('claim_checks', [])}
    for source in result['sources']:
        if isinstance(source.get('uri'), str) and source['uri'].startswith('10.'):
            source['uri'] = 'https://doi.org/' + source['uri']
    source_by_id = {s['id']: s for s in result['sources']}
    native_evidence = {e['id']: e for e in result['evidence']}
    canonical_evidence = {e['id']: e for e in payload.get('evidence', [])}
    independence = {}
    for item in payload.get('evidence', []):
        candidates = [c for c in audit.get('evidence_checks', []) if c.get('evidence_id') == item['id']]
        check = candidates[0] if len(candidates) == 1 else {}
        native = native_evidence[mapping['evidence'][item['id']]]
        def inspected_addressable(source_id):
            source = source_by_id[source_id]
            address = urlparse(source.get('uri') or '')
            return source.get('access') == 'inspected' and address.scheme in ('http', 'https') and bool(address.netloc)
        independent = bool(current and check.get('assessment') == 'independent' and not check.get('validation_issue')
            and check.get('reason', '').strip() and check.get('family_basis', '').strip() and check.get('study_family_ids')
            and not item.get('premise_claim_ids') and all(inspected_addressable(s) for s in native['source_ids']))
        native.update(independent=independent,
            premise_claim_ids=[mapping['claims'][i] for i in item.get('premise_claim_ids', [])],
            limitations=['Recorded evidence relation: ' + item.get('relation', 'unknown'),
                'Audited study-family assessment: ' + check.get('assessment', 'unknown') + '. ' + check.get('reason', '')])
        independence[native['id']] = dict(check, canary_evidence_id=item['id'])
    def uncertainty(value, evidence_ids):
        level = value.get('class')
        if level == 'low' and any(native_evidence[mapping['evidence'][e]]['independent'] and
                canonical_evidence[e].get('relation') == 'supports' for e in evidence_ids):
            return 'low'
        return {'low': 'medium', 'moderate': 'medium', 'high': 'high'}[level]
    for canonical, native in zip(payload.get('claims', []), result['claims']):
        native['class'] = 'observed_fact' if canonical.get('kind') == 'reported_finding' else 'derived_inference'
        native['uncertainty'] = uncertainty(canonical['uncertainty'], canonical['evidence_ids'])
        check = checks[canonical['id']]
        metadata['claim_metadata'][native['id']].update(final_verdict=check['assessment'], final_check=check,
            statement_sha256=check['statement_sha256'], claim_snapshot_sha256=check['claim_snapshot_sha256'])
    connectors = []
    for index, item in enumerate(payload.get('claim_links', []), 1):
        connectors.append({'id': 'x'+str(index), 'from': mapping['claims'][item['from_claim_id']],
            'to': mapping['claims'][item['to_claim_id']], 'type': item['kind'], 'spec': item['spec'],
            'reason': item['reason'], 'uncertainty': uncertainty(item['uncertainty'], item['evidence_ids']),
            'evidence_ids': [mapping['evidence'][i] for i in item['evidence_ids']]})
    def linked(record):
        return dict(record, canary_claim_ids=record.get('claim_ids', []), canary_evidence_ids=record.get('evidence_ids', []),
            claim_ids=[mapping['claims'][i] for i in record.get('claim_ids', [])],
            evidence_ids=[mapping['evidence'][i] for i in record.get('evidence_ids', [])])
    metadata.update(connector_version=5, uncertainty_policy=VERIFIED_UNCERTAINTY_POLICY,
        evidence_independence=independence,
        study_results=[dict(r, claim_ids=[mapping['claims'][c['id']] for c in payload['claims'] if r['id'] in c.get('study_result_ids', [])])
            for r in payload.get('study_results', [])],
        contradictions=[linked(r) for r in payload.get('contradictions', [])],
        withheld_claims=payload.get('withheld_claims', []), withheld_evidence=payload.get('withheld_evidence', []),
        withheld_study_results=payload.get('withheld_study_results', []),
        claim_revisions=[round_record(r) for r in payload.get('claim_revisions', [])],
        history_manifest=payload.get('history_manifest', []),
        verification={k: v for k, v in payload['verification'].items() if k != 'audit_basis'})
    result['connectors'] = connectors
    result['result']['limitations'] = [line for line in result['result']['limitations'] if line != UNCERTAINTY_POLICY
        and not line.startswith('Candidate claim classes require Resolvent admission;')]
    result['result']['limitations'].extend([VERIFIED_UNCERTAINTY_POLICY,
        'Observed facts are source-attributed reported findings, bounded by the stated study and audit scope; they are not universal truths. Resolvent still owns admission.'])
    verification = payload['verification']
    result['result']['limitations'].extend(
        ['Run-level audit finding (material; names no current claim, so no claim was withheld for it): ' + str(f.get('description', ''))
         for f in verification.get('run_findings', [])]
        + (['Verification stopped: ' + str(verification['failure']) + '. Every claim was withheld; working sources are kept.']
           if verification.get('failure') else [])
        + ([] if verification.get('audit_complete') else
           ['The final independent audit did not cover its full declared scope; see method_metadata.independent_audit.']))
    result['result']['limitations'] = list(dict.fromkeys(result['result']['limitations']))
    if not result['claims'] and payload.get('withheld_claims'):
        statuses = [r.get('status') for r in payload['withheld_claims']]
        result['handoff']['no_claims_reason'] = (f"No claim passed verification: {statuses.count('withheld')} withheld, "
            f"{statuses.count('withdrawn')} withdrawn. Reasons are in method_metadata.withheld_claims.")
    return result


EXPORTERS = {'resolvent': resolvent}


def export(workspace, connector, *, representation='compact'):
    """The compact contribution fits the destination limit or is refused; expanded is for reading."""
    from . import bundle_encoding
    if connector not in EXPORTERS:
        raise ValueError('Unsupported connector: ' + connector)
    if representation not in ('compact', 'expanded'):
        raise ValueError('Unsupported bundle representation')
    result = EXPORTERS[connector](workspace)
    if representation == 'expanded':
        return bundle_encoding.expand(result)
    result = compact(result)
    size = contribution(result)
    if not size['within_limit']:
        raise ContributionTooLarge(size['bytes'])
    return result
