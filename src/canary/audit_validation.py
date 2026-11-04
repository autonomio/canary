"""Bind model audit judgments to the exact scientific inputs they cover."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib

import jsonschema

from .research_spec import digest

STANDARD_VERSION = 1
# Deliberately broad: a shared record edit invalidates every dependent judgment.
SNAPSHOT_FIELDS = ('claims', 'evidence', 'study_results', 'references', 'contradictions',
                   'gaps', 'residue', 'claim_links', 'question_plan', 'selection', 'reviewed_papers',
                   'coverage', 'coverage_map', 'accounting', 'mechanical_audit', 'claim_review_checks')


def snapshot(payload):
    value = {field: payload.get(field) for field in SNAPSHOT_FIELDS}
    if isinstance(value.get('mechanical_audit'), dict):
        value['mechanical_audit'] = {k: v for k, v in value['mechanical_audit'].items() if k != 'at'}
    return digest(value)


def statement_hash(claim):
    return hashlib.sha256(claim['text'].encode('utf-8')).hexdigest()


def claim_hash(payload, claim):
    return digest({'snapshot_sha256': snapshot(payload), 'claim': claim})


def current(payload, claim, check):
    return (check.get('statement_sha256') == statement_hash(claim) and
            check.get('claim_snapshot_sha256') == claim_hash(payload, claim))



def matches_investigation(payload, investigation):
    from .coverage_protocol import investigation_schema
    shape = investigation_schema()['properties']
    for field in shape:
        left, right = payload.get(field), investigation.get(field)
        if field == 'evidence':
            keys = shape[field]['items']['properties']
            left = [{k: e[k] for k in keys if k in e} for e in (left or [])]
            right = [{k: e[k] for k in keys if k in e} for e in (right or [])]
        if left != right:
            return False
    return True

def bind(payload, raw, *, passages=()):
    """No fabricated checks; missing, ambiguous or invalid judgments stay explicit."""
    from .coverage_protocol import AUDIT_ISSUE_TYPES
    from .coverage_protocol import audit_schema
    audit = deepcopy(raw) if isinstance(raw, dict) else {}
    check_schema = audit_schema()['properties']['claim_checks']['items']
    evidence_schema = audit_schema()['properties']['evidence_checks']['items']
    def rows(value):
        return value if isinstance(value, list) else []
    def conforms(value, shape):
        return jsonschema.Draft202012Validator(shape).is_valid(value)

    claims = payload.get('claims', [])
    evidence = payload.get('evidence', [])
    references = {r['id']: r for r in payload.get('references', [])}
    passages = {p['id']: p for p in [*passages, *(p for e in evidence for p in e.get('verbatims', []))]}
    checks = [c for c in rows(audit.get('claim_checks')) if isinstance(c, dict) and isinstance(c.get('claim_id'), str)]
    counts = Counter(c.get('claim_id') for c in checks)
    claim_counts = Counter(c['id'] for c in claims)
    known = set(claim_counts)
    errors = ['Audit check uses unknown claim ID: ' + str(c.get('claim_id')) for c in checks if c.get('claim_id') not in known]
    reviewed = set(r for r in rows(audit.get('source_reviewed_ids')) if isinstance(r, str)) & references.keys()
    bound = []
    for claim in claims:
        identifier = claim['id']
        candidates = [c for c in checks if c.get('claim_id') == identifier]
        valid = counts[identifier] == 1 and claim_counts[identifier] == 1 and conforms(candidates[0], check_schema)
        check = deepcopy(candidates[0]) if valid else {
            'claim_id': identifier, 'assessment': 'not_checked', 'reason': 'No unique persisted independent check',
            'passage_ids': [], 'reference_ids': [], 'context_checked': [], 'issue_types': []}
        problems = []
        if check.get('assessment') not in ('supported', 'correction_needed', 'unresolved', 'not_checked'):
            problems.append('Invalid assessment')
        issues = check.get('issue_types')
        if not isinstance(issues, list) or any(i not in AUDIT_ISSUE_TYPES for i in issues):
            problems.append('Issue types missing or invalid')
            check['issue_types'] = ['other']
        ids, refs = check.get('passage_ids', []), check.get('reference_ids', [])
        if any(p not in passages for p in ids) or any(r not in references for r in refs):
            problems.append('Audit cites an unknown passage or reference')
        if check.get('assessment') == 'supported':
            if issues:
                check['assessment'] = 'correction_needed'
            if not ids or not refs or not check.get('context_checked') or not check.get('reason', '').strip():
                problems.append('Support lacks inspected source context or rationale')
            if any(r not in reviewed for r in refs):
                problems.append('Supporting reference not recorded as inspected')
            if any(passages.get(p, {}).get('reference_id') not in refs for p in ids):
                problems.append('Inspected passage and reference disagree')
        blockers = [r['message'] for r in payload.get('claim_review_checks', [])
                    if r.get('blocking') and identifier in r.get('claim_ids', [])]
        if blockers and check.get('assessment') == 'supported':
            check['assessment'] = 'correction_needed'
            check['issue_types'] = list(dict.fromkeys([*check['issue_types'], 'other']))
            check['reason'] += ' Structural check: ' + '; '.join(blockers)
        if problems:
            check['assessment'] = 'not_checked'
            check['reviewer_reason'] = check.get('reason')
            check['reason'] = 'Audit output failed validation: ' + '; '.join(problems)
            check['validation_issues'] = problems
            errors.extend(identifier + ': ' + p for p in problems)
        check.update(statement_sha256=statement_hash(claim), claim_snapshot_sha256=claim_hash(payload, claim))
        bound.append(check)
    independence, missing_independence = [], False
    evidence_checks = [c for c in rows(audit.get('evidence_checks')) if isinstance(c, dict)]
    for item in evidence:
        matches = [c for c in evidence_checks if c.get('evidence_id') == item['id']]
        valid = len(matches) == 1 and conforms(matches[0], evidence_schema)
        missing_independence = missing_independence or not valid
        check = deepcopy(matches[0]) if valid else {'evidence_id': item['id'], 'assessment': 'unknown',
            'reason': 'No unique persisted independence assessment', 'reference_ids': [], 'passage_ids': [],
            'study_family_ids': [], 'family_basis': ''}
        refs, ids = check.get('reference_ids', []), check.get('passage_ids', [])
        eligible = (check.get('assessment') in ('independent', 'same_study_family', 'unknown') and
            bool(check.get('reason', '').strip()) and bool(check.get('family_basis', '').strip()) and
            bool(refs) and bool(ids) and all(r in reviewed for r in refs) and
            bool(set(refs) & set(item.get('reference_ids', []))) and
            bool(set(ids) & set(item.get('passage_ids', []))) and
            all(passages.get(p, {}).get('reference_id') in refs for p in ids))
        if check.get('assessment') != 'unknown' and (not eligible or not check.get('study_family_ids')):
            check.update(assessment='unknown', validation_issue='Independence lacks inspected source/family basis')
        if check.get('assessment') == 'independent' and any(not (references[r].get('url') or references[r].get('doi') or references[r].get('record_url')) for r in refs):
            check.update(assessment='unknown', validation_issue='Independent evidence lacks an addressable source')
        if not valid:
            errors.append(item['id'] + ': No unique valid evidence-independence check')
        independence.append(check)
    audit.update(claim_checks=bound, evidence_checks=independence, validation_issues=errors,
        snapshot_sha256=snapshot(payload), standard_version=STANDARD_VERSION,
        audit_complete=audit.get('audit_complete') is True and not errors and not missing_independence and
            all(c['assessment'] != 'not_checked' for c in bound))
    return audit
