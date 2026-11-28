"""Dated editions of the study table and the claims that used each one."""
import hashlib
from pathlib import Path

from .coverage_runtime import now, read, write
from .research_spec import digest
from .study_table import _rows, _values, document_versions, value_documents

FILE = 'study-table-editions.json'


def _text(field):
    return field.get('text') if isinstance(field, dict) else None


def _value(field, owners, references):
    if not isinstance(field, dict):
        return None
    calculation = field.get('calculation') if isinstance(field.get('calculation'), dict) else None
    return {'text': field.get('text'), 'origin': field.get('origin'),
            'calculation': {'formula': calculation.get('formula'), 'assumptions': calculation.get('assumptions', []),
                            'inputs': [{k: i.get(k) for k in ('name', 'value', 'unit')} for i in _rows(calculation.get('inputs'))]}
            if calculation else None,
            'documents': [v['text_sha256'] for v in value_documents(field, owners, references)]}


def _details(result, owners, references):
    recorded = result.get('outcome_result') if isinstance(result.get('outcome_result'), dict) else {}
    details = {name: _text(recorded.get(name)) for name in ('qualitative', 'time_point', 'effect_measure')}
    availability = recorded.get('availability')
    details['availability'] = {k: availability.get(k) for k in ('status', 'basis')} if isinstance(availability, dict) else None
    favours = recorded.get('larger_value_favours')
    details['larger value favours'] = {k: favours.get(k) for k in ('group_id', 'basis')} if isinstance(favours, dict) else None
    details.update({name: _value(value, owners, references) for name, value in _values(result)})
    fields = result.get('fields') if isinstance(result.get('fields'), dict) else {}
    details.update({name: _text(field) for name, field in fields.items() if name != 'units'})
    return details


def contents(investigation, references, owners=None):
    """What an edition fixes: every recorded detail of each result and each study's count."""
    owners = owners or {}
    results = {}
    for result in _rows(investigation.get('study_results')):
        results[result.get('id')] = {
            'study_id': result.get('study_id'), 'outcome': result.get('outcome'),
            'unit': _text((result.get('fields') or {}).get('units')),
            'documents': document_versions(result.get('reference_ids', []), references),
            **_details(result, owners, references)}
    studies = {study.get('id'): {'subject_count': study.get('subject_count')}
               for study in _rows(investigation.get('studies'))}
    return {'results': results, 'studies': studies}


def _claims(investigation):
    return {claim.get('id'): {'text_sha256': hashlib.sha256(str(claim.get('text', '')).encode('utf-8')).hexdigest(),
                              'study_result_ids': list(claim.get('study_result_ids', []))}
            for claim in _rows(investigation.get('claims'))}


def _users(identifier, claims):
    return sorted((claim for claim, record in claims.items() if identifier in record['study_result_ids']), key=str)


def _summary_indexes(summary, claim_ids):
    return [index for index, entry in enumerate(summary if isinstance(summary, list) else [], 1)
            if isinstance(entry, dict) and set(entry.get('claim_ids', [])) & set(claim_ids)]


def _changes(before, after, claims, earlier_claims, summary):
    changes = []
    for identifier in dict.fromkeys([*before['results'], *after['results']]):
        old, new = before['results'].get(identifier), after['results'].get(identifier)
        if old == new:
            continue
        fields = sorted(key for key in (old or {}).keys() | (new or {}).keys() if (old or {}).get(key) != (new or {}).get(key))
        using = _users(identifier, claims)
        affected = sorted(set(_users(identifier, earlier_claims)) | set(using), key=str)
        changes.append({'study_result_id': identifier, 'added': old is None, 'removed': new is None, 'fields': fields,
                        'before': old, 'after': new, 'claim_ids': affected,
                        'unrevised_claim_ids': [c for c in using if c in earlier_claims
                                                and claims[c]['text_sha256'] == earlier_claims[c]['text_sha256']],
                        'summary_indexes': _summary_indexes(summary, affected)})
    for identifier in dict.fromkeys([*before['studies'], *after['studies']]):
        old, new = before['studies'].get(identifier), after['studies'].get(identifier)
        if old != new:
            changes.append({'study_id': identifier, 'added': old is None, 'removed': new is None,
                            'fields': ['subject_count'], 'before': old, 'after': new})
    return changes


def record(workspace: Path, investigation: dict, references: dict, owners: dict | None = None) -> dict:
    """Add an edition when the table changes; the claims of the current synthesis name it."""
    path = Path(workspace) / FILE
    editions = read(path, [])
    table = contents(investigation, references, owners)
    sha = digest(table)
    claims = _claims(investigation)
    if not editions or editions[-1]['sha256'] != sha:
        previous = editions[-1] if editions else None
        editions.append({'number': len(editions) + 1, 'sha256': sha, 'previous': previous['sha256'] if previous else None,
                         'recorded_at': now(), **table, 'claims': claims,
                         'changes': _changes(previous, table, claims, previous['claims'],
                                             investigation.get('executive_summary')) if previous else []})
        write(path, editions)
    current = editions[-1]
    return {'number': current['number'], 'sha256': current['sha256']}


def history(workspace: Path) -> list[dict]:
    return read(Path(workspace) / FILE, [])


def stale_claims(editions, investigation):
    """Current claims that still use a value an edition changed, without having been revised since."""
    current = _claims(investigation)
    found = []
    for earlier, edition in zip(editions, editions[1:]):
        for change in edition.get('changes', []):
            for claim in change.get('claim_ids', []):
                if claim in current and claim in earlier['claims'] and \
                        change['study_result_id'] in current[claim]['study_result_ids'] and \
                        current[claim]['text_sha256'] == earlier['claims'][claim]['text_sha256']:
                    found.append({'kind': 'claim_uses_changed_value', 'claim_id': claim,
                                  'study_result_id': change['study_result_id'], 'edition': edition['number'],
                                  'fields': change['fields'],
                                  'summary_indexes': _summary_indexes(investigation.get('executive_summary'), [claim])})
    return found
