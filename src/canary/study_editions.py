"""Dated editions of the study table and the claims that used each one."""
import hashlib
from pathlib import Path

from .coverage_runtime import now, read, write
from .research_spec import digest
from .study_table import CHARACTERISTICS, _rows, _values, document_versions, value_documents

FILE = 'study-table-editions.json'


def _cited(record, keys):
    return {**{k: record.get(k) for k in keys}, 'passages': list(record.get('passage_ids', []))} if isinstance(record, dict) else None


def _value(field, owners, references):
    if not isinstance(field, dict):
        return None
    calculation = field.get('calculation') if isinstance(field.get('calculation'), dict) else None
    return {'text': field.get('text'), 'origin': field.get('origin'),
            'calculation': {'formula': calculation.get('formula'), 'assumptions': calculation.get('assumptions', []),
                            'inputs': [_cited(i, ('name', 'value', 'unit')) for i in _rows(calculation.get('inputs'))]}
            if calculation else None,
            'documents': [v['text_sha256'] for v in value_documents(field, owners, references)],
            'passages': list(field.get('passage_ids', []))}


def _put(record, key, value):
    name, copy = key, 1
    while name in record:
        copy += 1
        name = f'{key} ({copy})'
    record[name] = value


def _without_passages(value):
    if isinstance(value, dict):
        return {k: _without_passages(v) for k, v in value.items() if k != 'passages'}
    if isinstance(value, list):
        return [_without_passages(v) for v in value]
    return value


def _details(result, owners, references):
    recorded = result.get('outcome_result') if isinstance(result.get('outcome_result'), dict) else {}
    details = {name: _value(recorded.get(name), owners, references) for name in ('qualitative', 'time_point', 'effect_measure')}
    details['availability'] = _cited(recorded.get('availability'), ('status', 'basis'))
    details['larger value favours'] = _cited(recorded.get('larger_value_favours'), ('group_id', 'basis'))
    details.update({name: _value(value, owners, references) for name, value in _values(result)})
    fields = result.get('fields') if isinstance(result.get('fields'), dict) else {}
    details.update({name: _value(field, owners, references) for name, field in fields.items() if name != 'units'})
    return details


def _study(study, owners, references):
    characteristics = study.get('characteristics') if isinstance(study.get('characteristics'), dict) else {}
    reports = _rows(study.get('reports'))
    record = {'citation': study.get('citation'), 'subject': study.get('subject'), 'subject_count': study.get('subject_count'),
              'reports': [_cited(r, ('reference_id', 'status', 'basis')) for r in reports],
              'documents': [v['text_sha256'] for v in document_versions([r.get('reference_id') for r in reports], references)]}
    record.update({name: _value(characteristics.get(name), owners, references) for name in CHARACTERISTICS})
    for group in _rows(study.get('groups')):
        _put(record, 'group ' + str(group.get('id')) + ' description', _value(group.get('description'), owners, references))
        _put(record, 'group ' + str(group.get('id')) + ' sample size', _value(group.get('sample_size'), owners, references))
    for variable in _rows(study.get('planned_variables')):
        _put(record, 'variable ' + str(variable.get('name')), _value(variable.get('value'), owners, references))
    for link in _rows(study.get('shared_with')):
        _put(record, f"shared with {link.get('study_id')} ({link.get('shares')})", _cited(link, ('shares', 'status', 'basis')))
    return record


def contents(investigation, references, owners=None):
    """What an edition fixes: every recorded detail of each result and each study."""
    owners = owners or {}
    results = {}
    for result in _rows(investigation.get('study_results')):
        results[result.get('id')] = {
            'study_id': result.get('study_id'), 'outcome': result.get('outcome'),
            'unit': _value((result.get('fields') or {}).get('units'), owners, references),
            'documents': document_versions(result.get('reference_ids', []), references),
            **_details(result, owners, references)}
    studies = {study.get('id'): _study(study, owners, references) for study in _rows(investigation.get('studies'))}
    return {'results': results, 'studies': studies}


def _claims(investigation):
    return {claim.get('id'): {'text_sha256': hashlib.sha256(str(claim.get('text', '')).encode('utf-8')).hexdigest(),
                              'study_result_ids': list(claim.get('study_result_ids', []))}
            for claim in _rows(investigation.get('claims'))}


def _users(identifier, claims):
    return sorted((claim for claim, record in claims.items()
                   if not record.get('withdrawn') and identifier in record['study_result_ids']), key=str)


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
        cited_only = _without_passages(old) == _without_passages(new)
        changes.append({'study_result_id': identifier, 'added': old is None, 'removed': new is None, 'fields': fields,
                        'before': old, 'after': new, 'claim_ids': affected, 'passages_only': cited_only,
                        'unrevised_claim_ids': [] if cited_only else [c for c in using if c in earlier_claims
                                                and claims[c]['text_sha256'] == earlier_claims[c]['text_sha256']],
                        'summary_indexes': _summary_indexes(summary, affected)})
    for identifier in dict.fromkeys([*before['studies'], *after['studies']]):
        old, new = before['studies'].get(identifier), after['studies'].get(identifier)
        if old != new:
            fields = sorted(key for key in (old or {}).keys() | (new or {}).keys() if (old or {}).get(key) != (new or {}).get(key))
            changes.append({'study_id': identifier, 'added': old is None, 'removed': new is None,
                            'fields': fields, 'before': old, 'after': new})
    return changes


def record(workspace: Path, investigation: dict, references: dict, owners: dict | None = None) -> dict:
    """Add an edition when the table changes; otherwise keep the latest wording of each claim on the current one."""
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
    else:
        kept = {claim: dict(record, withdrawn=True) for claim, record in editions[-1]['claims'].items() if claim not in claims}
        if {**kept, **claims} != editions[-1]['claims']:
            editions[-1]['claims'] = {**kept, **claims}
            write(path, editions)
    current = editions[-1]
    return {'number': current['number'], 'sha256': current['sha256']}


def history(workspace: Path) -> list[dict]:
    """Every recorded edition of the study table, oldest first."""
    return read(Path(workspace) / FILE, [])


def stale_claims(editions, investigation):
    """Current claims that still use a value an edition changed, without having been revised since."""
    current = _claims(investigation)
    found = []
    for earlier, edition in zip(editions, editions[1:]):
        for change in edition.get('changes', []):
            if change.get('passages_only'):
                continue
            for claim in change.get('claim_ids', []):
                if claim in current and claim in earlier['claims'] and \
                        change['study_result_id'] in current[claim]['study_result_ids'] and \
                        current[claim]['text_sha256'] == earlier['claims'][claim]['text_sha256']:
                    found.append({'kind': 'claim_uses_changed_value', 'claim_id': claim,
                                  'study_result_id': change['study_result_id'], 'edition': edition['number'],
                                  'fields': change['fields'],
                                  'summary_indexes': _summary_indexes(investigation.get('executive_summary'), [claim])})
    return found
