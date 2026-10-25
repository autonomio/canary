"""Claim review horizons describe reassessment, never automatic truth expiry."""
from datetime import date
from typing import Literal, TypedDict


class Freshness(TypedDict):
    policy: Literal['scheduled', 'event_driven', 'historical', 'unknown']
    as_of: str | None
    review_after: str | None
    triggers: list[str]
    rationale: str


def unknown() -> Freshness:
    return {'policy': 'unknown', 'as_of': None, 'review_after': None, 'triggers': [],
            'rationale': 'No review horizon was recorded for this claim.'}


def schema():
    return {'type': 'object', 'additionalProperties': False,
        'properties': {'policy': {'type': 'string', 'enum': ['scheduled', 'event_driven', 'historical', 'unknown']},
            'as_of': {'type': ['string', 'null']}, 'review_after': {'type': ['string', 'null']},
            'triggers': {'type': 'array', 'items': {'type': 'string'}}, 'rationale': {'type': 'string'}},
        'required': ['policy', 'as_of', 'review_after', 'triggers', 'rationale']}


def issues(value):
    if not isinstance(value, dict):
        return ['Freshness must be a typed record']
    result = []
    if set(value) != set(schema()['required']):
        result.append('Freshness fields are missing or unsupported')
    policy = value.get('policy')
    if policy not in ('scheduled', 'event_driven', 'historical', 'unknown'):
        result.append('Unknown freshness policy')
    for field in ('as_of', 'review_after'):
        if value.get(field) is not None:
            try:
                parsed = date.fromisoformat(value[field])
                if parsed.isoformat() != value[field]:raise ValueError
            except (TypeError, ValueError):
                result.append(field + ' must be an ISO date or null')
    triggers = value.get('triggers')
    if not isinstance(triggers, list) or any(not isinstance(x, str) or not x.strip() for x in triggers):
        result.append('Freshness triggers must be nonempty strings')
    if not isinstance(value.get('rationale'), str) or not value['rationale'].strip():
        result.append('Freshness rationale is required')
    if policy == 'scheduled' and not value.get('review_after'):
        result.append('Scheduled freshness requires a review_after date')
    if policy in ('historical', 'unknown', 'event_driven') and value.get('review_after') is not None:
        result.append('Only scheduled freshness has a review_after date')
    if policy == 'event_driven' and not triggers:
        result.append('Event-driven freshness requires a reassessment trigger')
    return result
