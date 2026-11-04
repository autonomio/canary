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
    known = {c['id'] for c in payload.get('claims', [])}
    selected = {c['claim_id'] for c in audit.get('claim_checks', []) if c.get('assessment') != 'supported'}
    selected.update(known - {c['claim_id'] for c in audit.get('claim_checks', [])})
    for finding in audit.get('findings', []):
        if finding.get('severity') == 'material':
            identifiers = set(finding.get('claim_ids', []))
            selected.update(identifiers if identifiers and identifiers <= known else known)
    invalid_evidence = {e['evidence_id'] for e in audit.get('evidence_checks', []) if e.get('validation_issue')}
    selected.update(c['id'] for c in payload.get('claims', []) if invalid_evidence & set(c.get('evidence_ids', [])))
    if (payload.get('mechanical_audit') or {}).get('pass') is not True:
        selected.update(known)
    selected.update(i for r in (payload.get('claim_review_checks') or []) if r.get('blocking') for i in r['claim_ids'])
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
    mechanical = payload.get('mechanical_audit') or {}
    eligible_ids = {c['id'] for c in payload.get('claims', []) if not failure and mechanical.get('pass') is True
        and c['id'] not in flagged and checks.get(c['id'], {}).get('assessment') == 'supported'
        and audit_validation.current(payload, c, checks[c['id']])}
    dependencies = {c['id']: set() for c in payload.get('claims', [])}
    evidence_by_id = {e['id']: e for e in payload.get('evidence', [])}
    for claim in payload.get('claims', []):
        for identifier in claim.get('evidence_ids', []):
            dependencies[claim['id']].update(evidence_by_id.get(identifier, {}).get('premise_claim_ids', []))
    for link in payload.get('claim_links', []):
        if link.get('kind') == 'dependency' and link.get('to_claim_id') in dependencies:
            dependencies[link['to_claim_id']].add(link.get('from_claim_id'))
            for eid in link.get('evidence_ids', []):
                dependencies[link['to_claim_id']].update(evidence_by_id.get(eid, {}).get('premise_claim_ids', []))
    while True:
        closed = {identifier for identifier in eligible_ids if dependencies[identifier] <= eligible_ids}
        if closed == eligible_ids:
            break
        eligible_ids = closed
    for claim in payload.get('claims', []):
        check = checks.get(claim['id'], {})
        eligible = claim['id'] in eligible_ids
        if eligible:
            accepted.append(deepcopy(claim))
        else:
            material = [f.get('description', '') for f in audit.get('findings', []) if f.get('severity') == 'material'
                and (not f.get('claim_ids') or claim['id'] in f['claim_ids'] or not set(f['claim_ids']) <= {c['id'] for c in payload.get('claims', [])})]
            reason = (failure or ('Source binding checks failed: ' + '; '.join(mechanical.get('issues', [])) if mechanical.get('pass') is not True else None)
                or ('Material finding remains: ' + '; '.join(material) if material else None)
                or ('Depends on withheld or unknown premise claims: ' + ', '.join(sorted(dependencies[claim['id']] - eligible_ids)) if dependencies[claim['id']] - eligible_ids else None)
                or (check.get('reason') if check.get('assessment') != 'supported' else None)
                or 'The audit has incomplete evidence assessments or a stale verdict')
            withheld.append({'claim': deepcopy(claim), 'status': 'withheld',
                'reason': reason, 'final_check': deepcopy(check),
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
    final['withheld_evidence'] = [e for e in final.get('evidence', []) if not set(e.get('premise_claim_ids', [])) <= active]
    final['evidence'] = [e for e in final.get('evidence', []) if set(e.get('premise_claim_ids', [])) <= active]
    active_evidence = {e['id'] for e in final['evidence']}
    final['executive_summary'] = [s for s in final.get('executive_summary', [])
        if s.get('claim_ids') and set(s['claim_ids']) <= active]
    for name in ('contradictions', 'gaps', 'residue'):
        for record in final.get(name, []):
            removed = [i for i in record.get('claim_ids', []) if i not in active]
            record['claim_ids'] = [i for i in record.get('claim_ids', []) if i in active]
            if removed:
                record['withheld_claim_ids'] = removed
            missing = [i for i in record.get('evidence_ids', []) if i not in active_evidence]
            record['evidence_ids'] = [i for i in record.get('evidence_ids', []) if i in active_evidence]
            if missing:
                record['withheld_evidence_ids'] = missing
    final['claim_links'] = [r for r in final.get('claim_links', [])
        if r.get('from_claim_id') in active and r.get('to_claim_id') in active and set(r.get('evidence_ids', [])) <= active_evidence]
    from .revision_artifacts import inventory
    final['history_manifest'] = inventory(history)
    final['research_status'] = 'verified_research_with_withheld_claims'
    verify_final(final)
    return final


def audit_basis(payload):
    """Validate final content against its audited basis without inventing a new verdict."""
    basis = payload.get('verification', {}).get('audit_basis')
    if not isinstance(basis, dict):
        return payload
    # Reinsert only the exact records filtered by finalization, never new or edited facts.
    current = deepcopy(basis)
    active = {c['id']: c for c in payload.get('claims', [])}
    known = {c['id'] for c in basis.get('claims', [])}
    withheld = {r['claim']['id'] for r in payload.get('withheld_claims', [])}
    current['claims'] = [active.get(c['id'], c) for c in basis.get('claims', [])]
    current['claims'].extend(c for identifier, c in active.items() if identifier not in known)
    if known - active.keys() - withheld:
        current['claims'] = payload.get('claims', [])  # Undocumented removal is not a valid projection.
    for field in audit_validation.SNAPSHOT_FIELDS:
        if field == 'claims':
            continue
        expected = deepcopy(basis.get(field))
        if field == 'evidence':
            expected = [e for e in (expected or []) if set(e.get('premise_claim_ids', [])) <= active.keys()]
        elif field in ('contradictions', 'gaps', 'residue') and isinstance(expected, list):
            for record in expected:
                removed = [i for i in record.get('claim_ids', []) if i not in active]
                record['claim_ids'] = [i for i in record.get('claim_ids', []) if i in active]
                if removed:
                    record['withheld_claim_ids'] = removed
                kept_evidence = {e['id'] for e in payload.get('evidence', [])}
                missing = [i for i in record.get('evidence_ids', []) if i not in kept_evidence]
                record['evidence_ids'] = [i for i in record.get('evidence_ids', []) if i in kept_evidence]
                if missing:
                    record['withheld_evidence_ids'] = missing
        elif field == 'claim_links':
            expected = [r for r in (expected or []) if r.get('from_claim_id') in active and r.get('to_claim_id') in active
                and set(r.get('evidence_ids', [])) <= {e['id'] for e in payload.get('evidence', [])}]
        if payload.get(field) != expected:
            current[field] = payload.get(field)
    return current



def verify_final(payload):
    if payload.get('verification', {}).get('version') != VERSION:
        raise ValueError('A verified final payload is required')
    basis = audit_basis(payload)
    audit = payload.get('independent_audit', {})
    checks = {c['claim_id']: c for c in audit.get('claim_checks', [])}
    if len(checks) != len(audit.get('claim_checks', [])):
        raise ValueError('Final audit has ambiguous duplicate claim checks')
    claims = payload.get('claims', [])
    active = {c['id'] for c in claims}
    evidence = {e['id']: e for e in payload.get('evidence', [])}
    results = {r['id'] for r in payload.get('study_results', [])}
    flagged = set(targets(basis, audit))
    if len(active) != len(claims) or len(evidence) != len(payload.get('evidence', [])):
        raise ValueError('Final claims and evidence require unique IDs')
    for claim in claims:
        check = checks.get(claim['id'], {})
        if (claim['id'] in flagged or check.get('assessment') != 'supported' or
                not audit_validation.current(basis, claim, check)):
            raise ValueError('Active claim lacks a current supported audit: ' + claim['id'])
        if not claim.get('evidence_ids') or not set(claim['evidence_ids']) <= evidence.keys():
            raise ValueError('Active claim has missing evidence: ' + claim['id'])
        if claim.get('kind') == 'reported_finding' and not set(claim.get('study_result_ids', [])) & results:
            raise ValueError('Empirical claim has no linked study result: ' + claim['id'])
    if any(not set(e.get('premise_claim_ids', [])) <= active for e in evidence.values()):
        raise ValueError('Active evidence refers to a withheld premise')
    for link in payload.get('claim_links', []):
        if not jsonschema.Draft202012Validator(protocol.investigation_schema()['properties']['claim_links']['items']).is_valid(link):
            raise ValueError('Claim link lacks its typed scientific basis')
        if not {link.get('from_claim_id'), link.get('to_claim_id')} <= active or not set(link.get('evidence_ids', [])) <= evidence.keys():
            raise ValueError('Claim link refers outside the active graph')
