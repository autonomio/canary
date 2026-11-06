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
    """Require explicit lineage for every rewrite, split or withdrawal.

    A rejection names every problem with its claim and passage IDs, so one retry can fix them all.
    """
    error = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(schema()).iter_errors(response))
    if error is not None:
        raise ValueError('Correction response does not match its schema at '
                         + ('/'.join(map(str, error.absolute_path)) or 'the top level') + ': ' + error.message[:300])
    old = {c['id']: c for c in payload['claims']}
    proposed = deepcopy(response['investigation'])
    new = {c['id']: c for c in proposed['claims']}
    problems = []
    def named(text, identifiers):
        if identifiers:
            problems.append(text + ': ' + ', '.join(identifiers))
    named('claim IDs used more than once', sorted({i for claims in (payload['claims'], proposed['claims'])
                                                  for i, n in Counter(c['id'] for c in claims).items() if n > 1}))
    counts = Counter(r['claim_id'] for r in response['responses'])
    named('more than one response for', sorted(i for i, n in counts.items() if n > 1))
    named('responses name claims that do not exist', sorted(counts.keys() - old.keys()))
    named('flagged claims without a response', [i for i in targets(payload, audit) if i not in counts])
    responses = {r['claim_id']: r for r in response['responses'] if r['claim_id'] in old}
    passages = {p['id'] for e in payload.get('evidence', []) for p in e.get('verbatims', [])}
    # The caller supplies all canonical IDs for source-based disputes/revisions.
    passages.update(payload.get('canonical_passage_ids', []))
    owned, withdrawn = Counter(), []
    for identifier, record in responses.items():
        if not record['reason'].strip():
            problems.append(identifier + ' gives no reason')
        named(identifier + ' cites passage IDs that are not source passages of this run',
              [p for p in record['passage_ids'] if p not in passages])
        replacements = record['replacement_claim_ids']
        if len(set(replacements)) != len(replacements):
            problems.append(identifier + ' repeats a replacement ID')
        if record['action'] == 'withdraw':
            if replacements or identifier in new:
                problems.append(identifier + ' is withdrawn but stays active or names replacements')
            withdrawn.append({'claim': deepcopy(old[identifier]), 'reason': record['reason'], 'status': 'withdrawn',
                'evidence': [deepcopy(e) for e in payload.get('evidence', []) if e['id'] in old[identifier].get('evidence_ids', [])],
                'study_results': [deepcopy(r) for r in payload.get('study_results', []) if r['id'] in old[identifier].get('study_result_ids', [])],
                'final_check': next((deepcopy(c) for c in audit.get('claim_checks', []) if c['claim_id'] == identifier), {})})
        elif record['action'] == 'dispute':
            if replacements or new.get(identifier) != old[identifier] or not record['passage_ids']:
                problems.append(identifier + ' is disputed, so it must stay unchanged, name no replacements and cite source passages')
        else:
            if not replacements or any(i not in new or i in old and i != identifier for i in replacements):
                problems.append(identifier + ' is revised, so it must name its replacement claims in the proposed '
                                'investigation without taking another claim ID')
            if identifier in new and identifier not in replacements:
                problems.append(identifier + ' is revised and stays active, so it must name itself as a replacement')
            owned.update(replacements)
    named('replacement claims with more than one parent', sorted(i for i, n in owned.items() if n != 1))
    named('changed or removed claims without a response', [i for i, c in old.items() if new.get(i) != c and i not in responses])
    named('new claims without a revision parent', sorted(i for i in new.keys() - old.keys() if i not in owned))
    if problems:
        raise ValueError('Correction response rejected: ' + '; '.join(problems))
    return proposed, withdrawn


def stopped_text(verification):
    """How a stopped verification left the claims, as reports and exports state it."""
    stopped = verification.get('stopped')
    if not stopped:  # Finalized before verification.stopped was recorded, when a stop withheld every claim.
        return 'Verification stopped: ' + str(verification['failure']) + '. Every claim was withheld; working sources are kept.'
    kept = ('No audit completed, so every claim was withheld.' if stopped['audit_round'] is None else
            f"Only claims audit round {stopped['audit_round']} passed, unchanged since, stay active; every other claim was withheld.")
    return (f"Verification stopped: {verification['failure']}. {stopped['kind'].capitalize()} round {stopped['round']} "
            f"did not finish. {kept} Working sources are kept.")


def problem(payload, audit, response):
    """Why apply() would reject this response, or None."""
    try:
        apply(payload, audit, response)
    except ValueError as error:
        return str(error)
    return None


def finalize(payload, audit, history, withdrawn=(), *, failure=None, stopped=None):
    """Keep failed claims visible, while closing the active graph over supported claims.

    A stopped verification (failure, and where it stopped) keeps the claims the last completed audit passed,
    unchanged since; nothing from the stopped session is accepted.
    """
    original = deepcopy(payload)
    checks = {c['claim_id']: c for c in audit.get('claim_checks', [])}
    flagged = set(targets(payload, audit))
    accepted, withheld = [], deepcopy(list(withdrawn))
    mechanical_issues = blocking_issues(payload)
    scoped_issues = claim_issues(payload)
    eligible_ids = {c['id'] for c in payload.get('claims', []) if not mechanical_issues
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
            reason = (('Source binding checks failed: ' + '; '.join(mechanical_issues) if mechanical_issues else None)
                or ('Claim record checks failed: ' + '; '.join(scoped_issues[claim['id']]) if scoped_issues.get(claim['id']) else None)
                or ('Material finding remains: ' + '; '.join(material) if material else None)
                or ('Depends on withheld or unknown premise claims: ' + ', '.join(sorted(dependencies[claim['id']] - eligible_ids)) if dependencies[claim['id']] - eligible_ids else None)
                or (check.get('reason') if check.get('assessment') != 'supported' else None)
                or ('No completed audit checked this claim' if not check else None)
                or 'The audit has incomplete evidence assessments or a stale verdict')
            if failure:
                reason = 'Verification stopped (' + failure + '). ' + reason
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
            **({'stopped': deepcopy(stopped)} if failure and stopped else {}),
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
