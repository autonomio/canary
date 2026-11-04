"""Consumer-neutral correction state and supported-only final claim projection."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy

import jsonschema

from . import audit_validation, coverage_protocol as protocol

VERSION = 1


def schema():
    return protocol.obj({'responses': protocol.arr(protocol.obj({
        'claim_id': protocol.S, 'action': protocol.enum('revise', 'withdraw', 'dispute'),
        'reason': protocol.S, 'passage_ids': protocol.SS, 'replacement_claim_ids': protocol.SS})),
        'investigation': protocol.investigation_schema()})


def targets(payload, audit):
    selected = {c['claim_id'] for c in audit.get('claim_checks', []) if c.get('assessment') != 'supported'}
    for finding in audit.get('findings', []):
        if finding.get('severity') == 'material':
            selected.update(finding.get('claim_ids') or [c['id'] for c in payload.get('claims', [])])
    selected.update(i for r in payload.get('claim_review_checks', []) if r.get('blocking') for i in r['claim_ids'])
    return [c['id'] for c in payload.get('claims', []) if c['id'] in selected]


def apply(payload, audit, response):
    """Require explicit lineage for every rewrite, split or withdrawal."""
    jsonschema.validate(response, schema())
    old = {c['id']: c for c in payload['claims']}
    proposed = deepcopy(response['investigation'])
    new = {c['id']: c for c in proposed['claims']}
    if len(old) != len(payload['claims']) or len(new) != len(proposed['claims']):
        raise ValueError('Correction cannot use duplicate claim IDs')
    responses = {r['claim_id']: r for r in response['responses']}
    if len(responses) != len(response['responses']) or not set(responses) <= old.keys():
        raise ValueError('Correction responses require unique existing claim IDs')
    if not set(targets(payload, audit)) <= responses.keys():
        raise ValueError('Every flagged claim requires an explicit correction response')
    passages = {p['id'] for e in payload.get('evidence', []) for p in e.get('verbatims', [])}
    # The caller supplies all canonical IDs for source-based disputes/revisions.
    passages.update(payload.get('canonical_passage_ids', []))
    owned, withdrawn = Counter(), []
    for identifier, record in responses.items():
        if not record['reason'].strip() or any(p not in passages for p in record['passage_ids']):
            raise ValueError('Correction response requires a reason and valid source passage IDs')
        replacements = record['replacement_claim_ids']
        if len(set(replacements)) != len(replacements):
            raise ValueError('Replacement IDs must be unique')
        if record['action'] == 'withdraw':
            if replacements or identifier in new:
                raise ValueError('Withdrawn claim cannot remain active or name replacements')
            withdrawn.append({'claim': deepcopy(old[identifier]), 'reason': record['reason'], 'status': 'withdrawn',
                'evidence': [deepcopy(e) for e in payload.get('evidence', []) if e['id'] in old[identifier].get('evidence_ids', [])],
                'final_check': next((deepcopy(c) for c in audit.get('claim_checks', []) if c['claim_id'] == identifier), {})})
        elif record['action'] == 'dispute':
            if replacements or new.get(identifier) != old[identifier] or not record['passage_ids']:
                raise ValueError('Dispute must preserve the claim and cite original source passages')
        else:
            if not replacements or any(i not in new or i in old and i != identifier for i in replacements):
                raise ValueError('Revision must identify its replacement claims without taking another claim ID')
            if identifier in new and identifier not in replacements:
                raise ValueError('Revised parent remaining active must be named as a replacement')
            owned.update(replacements)
    if any(count != 1 for count in owned.values()):
        raise ValueError('A replacement claim must have exactly one parent')
    for identifier, claim in old.items():
        if new.get(identifier) != claim and identifier not in responses:
            raise ValueError('Changed or removed claim lacks a response: ' + identifier)
    if any(identifier not in owned for identifier in new.keys() - old.keys()):
        raise ValueError('New claim lacks revision lineage')
    return proposed, withdrawn


def finalize(payload, audit, history, withdrawn=(), *, failure=None):
    """Keep failed claims visible, while closing the active graph over supported claims."""
    original = deepcopy(payload)
    checks = {c['claim_id']: c for c in audit.get('claim_checks', [])}
    flagged = set(targets(payload, audit))
    accepted, withheld = [], deepcopy(list(withdrawn))
    mechanical = payload.get('mechanical_audit', {})
    for claim in payload.get('claims', []):
        check = checks.get(claim['id'], {})
        eligible = (not failure and mechanical.get('pass') is True and claim['id'] not in flagged and
            check.get('assessment') == 'supported' and audit_validation.current(payload, claim, check))
        if eligible:
            accepted.append(deepcopy(claim))
        else:
            withheld.append({'claim': deepcopy(claim), 'status': 'withheld',
                'reason': failure or check.get('reason') or 'No current supported verdict', 'final_check': deepcopy(check),
                'evidence': [deepcopy(e) for e in payload.get('evidence', []) if e['id'] in claim.get('evidence_ids', [])]})
    active = {c['id'] for c in accepted}
    # Entire shared snapshots were audited. Keep the basis for verifiable hashes after filtering.
    final = deepcopy(payload)
    final.update(claims=accepted, withheld_claims=withheld, claim_revisions=deepcopy(history),
        independent_audit=deepcopy(audit), verification={'version': VERSION,
            'audit_basis': {k: original.get(k) for k in audit_validation.SNAPSHOT_FIELDS},
            'candidate_claims': len(payload.get('claims', [])) + len(withdrawn),
            'supported_claims': len(accepted), 'withheld_claims': len(withheld),
            'withheld_share': len(withheld)/(len(accepted)+len(withheld)) if accepted or withheld else 0,
            'correction_rounds': sum(h.get('kind') == 'correction' for h in history),
            'audit_complete': audit.get('audit_complete', False), 'failure': failure})
    final['executive_summary'] = [s for s in final.get('executive_summary', [])
        if s.get('claim_ids') and set(s['claim_ids']) <= active]
    for name in ('contradictions', 'gaps', 'residue'):
        for record in final.get(name, []):
            removed = [i for i in record.get('claim_ids', []) if i not in active]
            record['claim_ids'] = [i for i in record.get('claim_ids', []) if i in active]
            if removed:
                record['withheld_claim_ids'] = removed
    final['claim_links'] = [r for r in final.get('claim_links', [])
        if r.get('from_claim_id') in active and r.get('to_claim_id') in active]
    final['research_status'] = 'verified_research_with_withheld_claims'
    return final


def audit_basis(payload):
    """Validate final content against its audited basis without inventing a new verdict."""
    basis = payload.get('verification', {}).get('audit_basis')
    if not isinstance(basis, dict):
        return payload
    # Active statements and evidence must still match the audited snapshot.
    current = deepcopy(basis)
    active = {c['id']: c for c in payload.get('claims', [])}
    current['claims'] = [active.get(c['id'], c) for c in basis.get('claims', [])]
    for field in ('evidence', 'study_results', 'references'):
        current[field] = payload.get(field)
    return current
