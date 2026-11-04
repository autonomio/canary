"""Cheap claim checks: structural failures and explicitly nonsemantic review cues."""
from collections import defaultdict
import re

from .research_spec import digest

CONTEXT = re.compile(r'\b(?:that same|that study|the same cohort|the reviewed evidence|this corpus)\b', re.I)


def check(payload):
    results = {r['id']: r for r in payload.get('study_results', [])}
    records, repeated = [], defaultdict(set)
    for claim in payload.get('claims', []):
        linked = [results[r] for r in claim.get('study_result_ids', []) if r in results]
        if claim.get('kind') == 'reported_finding' and not linked:
            records.append({'kind': 'missing_study_result', 'claim_ids': [claim['id']], 'blocking': True,
                'message': 'Empirical reported finding has no linked typed study result.'})
        matches = sorted({m.group(0) for m in CONTEXT.finditer(claim.get('text', ''))})
        if matches:
            records.append({'kind': 'context_wording', 'claim_ids': [claim['id']], 'blocking': False,
                'message': 'Review possible unresolved references: ' + ', '.join(matches) + '. This cue alone does not establish a defect.'})
        for result in linked:
            fields = {k: v.get('text') if isinstance(v, dict) else None for k, v in result.get('fields', {}).items()}
            if not fields.get('value') or not result.get('reference_ids'):
                continue
            key = digest({'references': sorted(result['reference_ids']), 'fields': fields,
                'result_type': result.get('result_type'), 'version_or_cohort_note': result.get('version_or_cohort_note')})
            repeated[key].add(claim['id'])
    for ids in repeated.values():
        if len(ids) > 1:
            records.append({'kind': 'possible_duplicate', 'claim_ids': sorted(ids), 'blocking': False,
                'message': 'Claims cite the same reference and typed value/context fields. Review whether they express distinct propositions; code does not merge them.'})
    return records
