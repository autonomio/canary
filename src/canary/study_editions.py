"""Dated editions of the study table and the claims that used each one."""
import hashlib
from pathlib import Path

from .coverage_runtime import now, read, write
from .research_spec import digest
from .study_table import _rows, _values, document_versions

FILE = 'study-table-editions.json'


def _text(field):
    return field.get('text') if isinstance(field, dict) else None


def contents(investigation, references):
    """What an edition fixes: each result's values, unit and report copies, and each study's count."""
    results = {}
    for result in _rows(investigation.get('study_results')):
        results[result.get('id')] = {
            'study_id': result.get('study_id'), 'outcome': result.get('outcome'),
            'unit': _text((result.get('fields') or {}).get('units')),
            'values': {name: _text(value) for name, value in _values(result)},
            'documents': document_versions(result.get('reference_ids', []), references)}
    studies = {study.get('id'): {'subject_count': study.get('subject_count')}
               for study in _rows(investigation.get('studies'))}
    return {'results': results, 'studies': studies}


def _claims(investigation):
    return {claim.get('id'): {'text_sha256': hashlib.sha256(str(claim.get('text', '')).encode('utf-8')).hexdigest(),
                              'study_result_ids': list(claim.get('study_result_ids', []))}
            for claim in _rows(investigation.get('claims'))}


def _changes(before, after, claims, earlier_claims, summary):
    changes = []
    for identifier in dict.fromkeys([*before['results'], *after['results']]):
        old, new = before['results'].get(identifier), after['results'].get(identifier)
        if old is None or old == new:
            continue
        fields = sorted(key for key in old.keys() | (new or {}).keys() if old.get(key) != (new or {}).get(key))
        if new is not None and fields == ['values']:
            fields = sorted('values.' + key for key in old['values'].keys() | new['values'].keys()
                            if old['values'].get(key) != new['values'].get(key))
        affected = sorted({claim for claim, record in {**earlier_claims, **claims}.items()
                           if identifier in record['study_result_ids']}, key=str)
        changes.append({'study_result_id': identifier, 'removed': new is None, 'fields': fields,
                        'before': old, 'after': new, 'claim_ids': affected,
                        'unrevised_claim_ids': [c for c in affected if c in claims and c in earlier_claims
                                                and claims[c]['text_sha256'] == earlier_claims[c]['text_sha256']],
                        'summary_indexes': [index for index, entry in enumerate(summary if isinstance(summary, list) else [], 1)
                                            if isinstance(entry, dict) and set(entry.get('claim_ids', [])) & set(affected)]})
    return changes


def record(workspace: Path, investigation: dict, references: dict) -> dict:
    """Add an edition when the table changes; the claims of the current synthesis name it."""
    path = Path(workspace) / FILE
    editions = read(path, [])
    table = contents(investigation, references)
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
                        current[claim]['text_sha256'] == earlier['claims'][claim]['text_sha256']:
                    found.append({'kind': 'claim_uses_changed_value', 'claim_id': claim,
                                  'study_result_id': change['study_result_id'], 'edition': edition['number'],
                                  'fields': change['fields']})
    return found
