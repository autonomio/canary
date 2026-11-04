"""Consumer-neutral correction state and supported-only final claim projection."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy

import jsonschema

from . import audit_validation, coverage_protocol as protocol

VERSION = 1
# Active-graph projection rule. Rule 2 also moves evidence and study results that only withheld or
# withdrawn claims use to withheld records; payloads finalized earlier keep their recorded rule 1.
PROJECTION = 2


def schema():
    return protocol.obj({'responses': protocol.arr(protocol.obj({
        'claim_id': protocol.S, 'action': protocol.enum('revise', 'withdraw', 'dispute'),
        'reason': protocol.S, 'passage_ids': protocol.SS, 'replacement_claim_ids': protocol.SS})),
        'investigation': protocol.investigation_schema()})


def blocking_issues(payload):
    """Mechanical failures shared by every claim, such as source hash or graph defects."""
    mechanical = payload.get("mechanical_audit") or {}
    return mechanical.get("claim_blocking_issues", mechanical.get("issues", ["Source binding was not checked"])
        if mechanical.get("pass") is not True else [])


def claim_issues(payload):
    """Record failures of one claim, by claim ID; each withholds only its own claim."""
    mechanical = payload.get("mechanical_audit") or {}
    return {} if mechanical.get("pass") is True else mechanical.get("claim_issues") or {}


def run_findings(payload, audit):
    """Material findings naming no current or audited claim: disclosed for the run, never a claim verdict."""
    # A stopped later audit leaves an earlier audit final; claims it checked were since corrected, not unknown.
    known = {c['id'] for c in payload.get('claims', [])} | {c.get('claim_id') for c in audit.get('claim_checks', [])}
    return [deepcopy(f) for f in audit.get('findings', [])
            if f.get('severity') == 'material' and not set(f.get('claim_ids') or []) & known]


def withheld_only(payload, active, withdrawn=()):
    """Evidence and study-result IDs that only withheld or withdrawn claims, or their dropped links, use."""
    active = set(active)
    claims = payload.get('claims') or []
    links = payload.get('claim_links') or []
    retired = [c for c in claims if c['id'] not in active] + [r['claim'] for r in withdrawn]
    def kept(link):
        return {link.get('from_claim_id'), link.get('to_claim_id')} <= active
    # Contradiction, gap and residue records keep their evidence while they name an active claim or no claim.
    records = [r for name in ('contradictions', 'gaps', 'residue') for r in payload.get(name) or []]
    def live(record):
        return not record.get('claim_ids') or bool(set(record['claim_ids']) & active)
    used = {e for c in claims if c['id'] in active for e in c.get('evidence_ids', [])}
    used.update(e for r in links if kept(r) for e in r.get('evidence_ids', []))
    used.update(e for r in records if live(r) for e in r.get('evidence_ids', []))
    hidden = {e for c in retired for e in c.get('evidence_ids', [])}
    hidden.update(e for r in links if not kept(r) for e in r.get('evidence_ids', []))
    hidden.update(e for r in records if not live(r) for e in r.get('evidence_ids', []))
    results = {s for c in claims if c['id'] in active for s in c.get('study_result_ids', [])}
    return hidden - used, {s for c in retired for s in c.get('study_result_ids', [])} - results


def projection(payload):
    return payload.get('verification', {}).get('projection_version', 1)


def projected_evidence(payload, active, withdrawn=(), rule=PROJECTION):
    rows = payload.get("evidence") or []
    counts = Counter(e["id"] for e in rows)
    hidden = withheld_only(payload, active, withdrawn)[0] if rule >= 2 else set()
    return [e for e in rows if counts[e["id"]] == 1 and set(e.get("premise_claim_ids", [])) <= active
            and e["id"] not in hidden]


def projected_results(payload, active, withdrawn=()):
    _, hidden = withheld_only(payload, active, withdrawn)
    return [r for r in payload.get('study_results') or [] if r['id'] not in hidden]


def targets(payload, audit):
    known = {c['id'] for c in payload.get('claims', [])}
    selected = {c['claim_id'] for c in audit.get('claim_checks', []) if c.get('assessment') != 'supported'}
    selected.update(known - {c['claim_id'] for c in audit.get('claim_checks', [])})
    for finding in audit.get('findings', []):
        if finding.get('severity') == 'material':
            # Unknown or absent IDs never widen a finding to every claim; see run_findings.
            selected.update(set(finding.get('claim_ids') or []) & known)
    invalid_evidence = {e['evidence_id'] for e in audit.get('evidence_checks', []) if e.get('validation_issue')}
    selected.update(c['id'] for c in payload.get('claims', []) if invalid_evidence & set(c.get('evidence_ids', [])))
    if blocking_issues(payload):
        selected.update(known)
    selected.update(identifier for identifier, issues in claim_issues(payload).items() if issues)
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
                'study_results': [deepcopy(r) for r in payload.get('study_results', []) if r['id'] in old[identifier].get('study_result_ids', [])],
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
    mechanical_issues = blocking_issues(payload)
    scoped_issues = claim_issues(payload)
    eligible_ids = {c['id'] for c in payload.get('claims', []) if not failure and not mechanical_issues
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
                and claim['id'] in (f.get('claim_ids') or [])]
            reason = (failure or ('Source binding checks failed: ' + '; '.join(mechanical_issues) if mechanical_issues else None)
                or ('Claim record checks failed: ' + '; '.join(scoped_issues[claim['id']]) if scoped_issues.get(claim['id']) else None)
                or ('Material finding remains: ' + '; '.join(material) if material else None)
                or ('Depends on withheld or unknown premise claims: ' + ', '.join(sorted(dependencies[claim['id']] - eligible_ids)) if dependencies[claim['id']] - eligible_ids else None)
                or (check.get('reason') if check.get('assessment') != 'supported' else None)
                or 'The audit has incomplete evidence assessments or a stale verdict')
            withheld.append({'claim': deepcopy(claim), 'status': 'withheld',
                'reason': reason, 'final_check': deepcopy(check),
                'evidence': [deepcopy(e) for e in payload.get('evidence', []) if e['id'] in claim.get('evidence_ids', [])],
                'study_results': [deepcopy(r) for r in payload.get('study_results', []) if r['id'] in claim.get('study_result_ids', [])]})
    active = {c['id'] for c in accepted}
    scoped = {issue for issues in scoped_issues.values() for issue in issues}
    # Entire shared snapshots were audited. Keep the basis for verifiable hashes after filtering.
    final = deepcopy(payload)
    final.update(claims=accepted, withheld_claims=withheld, claim_revisions=deepcopy(history),
        independent_audit=deepcopy(audit), verification={'version': VERSION, 'projection_version': PROJECTION,
            'audit_basis': {k: original.get(k) for k in audit_validation.SNAPSHOT_FIELDS},
            'candidate_claims': len(payload.get('claims', [])) + len(withdrawn),
            'supported_claims': len(accepted), 'withheld_claims': len(withheld),
            'withheld_share': len(withheld)/(len(accepted)+len(withheld)) if accepted or withheld else 0,
            'correction_rounds': sum(h.get('kind') == 'correction' for h in history),
            'audit_complete': audit.get('audit_complete', False), 'failure': failure,
            'run_issues': [issue for issue in (payload.get('mechanical_audit') or {}).get('issues', [])
                           if issue not in mechanical_issues and issue not in scoped],
            'run_findings': run_findings(payload, audit)})
    # Evidence and study results used only by withheld or withdrawn claims leave the active graph.
    final['evidence'] = projected_evidence(payload, active, withdrawn)
    final['withheld_evidence'] = [e for e in payload.get('evidence', []) if e not in final['evidence']]
    final['study_results'] = projected_results(payload, active, withdrawn)
    final['withheld_study_results'] = [r for r in payload.get('study_results', []) if r not in final['study_results']]
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
    withdrawn = [r for r in payload.get('withheld_claims', []) if r.get('status') == 'withdrawn']
    rule = projection(payload)
    current['claims'] = [active.get(c['id'], c) for c in basis.get('claims', [])]
    current['claims'].extend(c for identifier, c in active.items() if identifier not in known)
    if known - active.keys() - withheld:
        current['claims'] = payload.get('claims', [])  # Undocumented removal is not a valid projection.
    for field in audit_validation.SNAPSHOT_FIELDS:
        if field == 'claims':
            continue
        expected = deepcopy(basis.get(field))
        if field == 'evidence':
            expected = projected_evidence(basis, active.keys(), withdrawn, rule)
        elif field == 'study_results' and rule >= 2:
            expected = projected_results(basis, active.keys(), withdrawn)
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
    hidden_evidence, hidden_results = withheld_only(basis, active,
        [r for r in payload.get('withheld_claims', []) if r.get('status') == 'withdrawn'])
    if projection(payload) >= 2 and (hidden_evidence & evidence.keys() or hidden_results & results):
        raise ValueError('Active output keeps evidence or study results used only by withheld claims')
    for link in payload.get('claim_links', []):
        if not jsonschema.Draft202012Validator(protocol.investigation_schema()['properties']['claim_links']['items']).is_valid(link):
            raise ValueError('Claim link lacks its typed scientific basis')
        if not {link.get('from_claim_id'), link.get('to_claim_id')} <= active or not set(link.get('evidence_ids', [])) <= evidence.keys():
            raise ValueError('Claim link refers outside the active graph')
