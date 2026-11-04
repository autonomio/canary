"""Separate audit recommendations; never rewrite the researcher's uncertainty."""

LEVELS = {'low': 1, 'moderate': 2, 'high': 3}


def schema():
    grade = {'type': ['string', 'null'], 'enum': [*LEVELS, None]}
    properties = {'recommended_class': grade, 'reason': {'type': 'string'},
        'evidence_ids': {'type': 'array', 'items': {'type': 'string'}},
        'prior_claim_id': {'type': ['string', 'null']}, 'prior_class': grade,
        'prior_match_reason': {'type': 'string'}}
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def unassessed(reason='No uncertainty review was recorded.'):
    return {'recommended_class': None, 'reason': reason, 'evidence_ids': [],
            'prior_claim_id': None, 'prior_class': None, 'prior_match_reason': ''}


def _prior_records(state, identifier):
    # The supplied snapshot is intentionally opaque; only identifiable claim
    # records with an explicit proposition and recorded grade can be compared.
    stack = [state]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.get('id') == identifier and (value.get('statement') or value.get('text')):
                yield value
            stack.extend(v for v in value.values() if isinstance(v, (dict, list)))
        elif isinstance(value, list):
            stack.extend(v for v in value if isinstance(v, (dict, list)))


def review(check, *, state=None, evidence_ids=()):
    """Project saved recommendations and derive ordinal change from verified input.

    A prior match remains the auditor's proposed semantic match. This function
    verifies its ID and recorded grade, not semantic equivalence or independence.
    Raw audit records remain available even if a recommendation cannot be used.
    """
    check = check if isinstance(check, dict) else {}
    raw = check.get('uncertainty_review')
    raw = raw if isinstance(raw, dict) else unassessed()
    result = unassessed('')
    for key in ('reason', 'prior_claim_id', 'prior_match_reason'):
        if isinstance(raw.get(key), str):
            result[key] = raw[key]
    for key in ('recommended_class', 'prior_class'):
        if isinstance(raw.get(key), str) and raw[key] in LEVELS:
            result[key] = raw[key]
    basis = raw.get('evidence_ids')
    if isinstance(basis, list):
        result['evidence_ids'] = [e for e in basis if isinstance(e, str)]
    result.update(status='not_assessed', change='not_comparable', limitations=[])
    grade = result['recommended_class']
    if (check.get('assessment') not in ('supported', 'correction_needed', 'unresolved', 'corrected')
            or grade is None or not result['reason'].strip()):
        result['recommended_class'] = None
        return result
    if (not isinstance(basis, list) or any(not isinstance(e, str) or e not in evidence_ids for e in basis)):
        result['recommended_class'] = None
        result['limitations'].append('Uncertainty recommendation cites unavailable evidence IDs.')
        return result
    result['status'] = 'assessed'
    identifier = result['prior_claim_id']
    if not identifier:
        return result
    records = list(_prior_records(state, identifier))
    if len(records) != 1:
        result['limitations'].append('Prior claim ID is missing or ambiguous in the supplied state; comparison unavailable.')
        return result
    prior = records[0].get('uncertainty')
    prior = prior.get('class') if isinstance(prior, dict) else prior
    prior = 'moderate' if prior == 'medium' else prior
    if (not isinstance(prior, str) or prior not in LEVELS or prior != result['prior_class']
            or not result['prior_match_reason'].strip()):
        result['limitations'].append('Prior grade or same-scope match was not established from the supplied state; comparison unavailable.')
        return result
    delta = LEVELS[grade] - LEVELS[prior]
    if delta < 0 and not basis:
        result['limitations'].append('No new evidence was identified for a decrease in uncertainty; comparison unavailable.')
        return result
    result['change'] = 'increased' if delta > 0 else 'decreased' if delta < 0 else 'unchanged'
    return result
