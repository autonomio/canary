"""Syntheses: how several study results become one finding, checked against the study table."""
from collections import Counter

from .study_table import _outcome_name, _rows

DECISIONS = ('combined', 'kept_separate')
METHODS = ('narrative', 'swim', 'statistical', 'imported', 'other')
IN_WORDS = ('narrative', 'swim')
SWIM_TECHNIQUES = ('summary_of_effects', 'combined_p_values', 'vote_counting_direction')
EXPLANATION = ('direction', 'differences', 'design_and_bias')


def _blank(value):
    return not isinstance(value, str) or not value.strip()


def _estimated(result):
    estimate = (result.get('outcome_result') or {}).get('estimate')
    return isinstance(estimate, dict) and not _blank(estimate.get('text'))


def _in_words(synthesis, contributing, owner):
    kind = (synthesis.get('method') or {}).get('kind')
    explanation = synthesis.get('explanation')
    found = []
    if kind in IN_WORDS and not isinstance(explanation, dict):
        found.append({'kind': 'synthesis_without_explanation', **owner})
    if isinstance(explanation, dict):
        found.extend({'kind': 'explanation_missing', **owner, 'field': name} for name in EXPLANATION if _blank(explanation.get(name)))
        if _blank(explanation.get('size')) and any(_estimated(r) for r in contributing):
            found.append({'kind': 'explanation_missing', **owner, 'field': 'size'})
    if kind == 'swim':
        swim = synthesis.get('swim')
        if not isinstance(swim, dict):
            found.append({'kind': 'swim_without_details', **owner})
        else:
            found.extend({'kind': 'swim_missing', **owner, 'field': name} for name in ('prioritisation', 'limitations')
                         if _blank(swim.get(name)))
            if swim.get('technique') == 'summary_of_effects' and contributing and not any(_estimated(r) for r in contributing):
                found.append({'kind': 'swim_without_effect_estimates', **owner})
    return found


def _contributing(synthesis, results):
    return [results[i] for i in dict.fromkeys(synthesis.get('study_result_ids', [])) if i in results]


def issues(syntheses, results, claims, versions):
    """Mechanical defects in each synthesis; whether the grouping is sound stays with the audit."""
    syntheses = _rows(syntheses)
    results = {r.get('id'): r for r in _rows(results)}
    claim_ids = {c.get('id') for c in _rows(claims)}
    known = Counter(s.get('id') for s in syntheses)
    outcomes = {_outcome_name(o.get('name', '')): o.get('id') for o in _rows(versions[-1].get('outcomes'))} if versions else None
    found = [{'kind': 'duplicate_synthesis_id', 'synthesis_id': key} for key, count in known.items() if count > 1]
    for synthesis in syntheses:
        owner = {'synthesis_id': synthesis.get('id')}
        cited = list(dict.fromkeys(synthesis.get('study_result_ids', [])))
        if not cited:
            found.append({'kind': 'synthesis_without_results', **owner})
        found.extend({'kind': 'unknown_synthesis_result', **owner, 'study_result_id': r} for r in cited if r not in results)
        outcome = synthesis.get('outcome')
        if isinstance(outcome, str) and outcomes is not None and _outcome_name(outcome) not in outcomes:
            found.append({'kind': 'unknown_synthesis_outcome', **owner, 'outcome_name': outcome})
        expected = outcomes.get(_outcome_name(outcome)) if isinstance(outcome, str) and outcomes else None
        names = {identifier: name for name, identifier in (outcomes or {}).items()}
        for result in _contributing(synthesis, results):
            if not isinstance(outcome, str):
                continue
            linked = result.get('outcome') if isinstance(result.get('outcome'), dict) else {}
            named = linked.get('name') or names.get(linked.get('id'))
            if expected is not None and linked.get('id') is not None:
                same = linked['id'] == expected
            else:
                same = _outcome_name(named or '') == _outcome_name(outcome)
            if not same:
                found.append({'kind': 'synthesis_outcome_mismatch', **owner, 'study_result_id': result.get('id'),
                              'synthesis_outcome': outcome, 'result_outcome': named})
        grouping = synthesis.get('grouping') if isinstance(synthesis.get('grouping'), dict) else {}
        if _blank(grouping.get('reason')):
            found.append({'kind': 'grouping_without_reason', **owner})
        found.extend(_in_words(synthesis, _contributing(synthesis, results), owner))
        if grouping.get('decision') == 'combined':
            studies = Counter(r.get('study_id') for r in _contributing(synthesis, results) if r.get('study_id') is not None)
            found.extend({'kind': 'repeated_synthesis_study', **owner, 'study_id': key} for key, count in studies.items() if count > 1)
        found.extend({'kind': 'unknown_kept_apart_synthesis', **owner, 'kept_apart_from': key}
                     for key in grouping.get('kept_apart_from', []) if key not in known or key == synthesis.get('id'))
        found.extend({'kind': 'unknown_synthesis_claim', **owner, 'claim_id': c}
                     for c in synthesis.get('claim_ids', []) if c not in claim_ids)
    return found


def export(syntheses, results):
    """Each synthesis with the studies its results come from, each counted once."""
    results = {r.get('id'): r for r in _rows(results)}
    return [dict(s, study_ids=list(dict.fromkeys(r.get('study_id') for r in _contributing(s, results)
                                                if r.get('study_id') is not None)))
            for s in _rows(syntheses)]


def stale(editions, investigation):
    """Syntheses left unchanged after a study result they use changed, with the input that changed."""
    current = {s.get('id'): s for s in _rows(investigation.get('syntheses'))}
    found = []
    for earlier, edition in zip(editions, editions[1:]):
        before = earlier.get('syntheses') or {}
        for change in edition.get('changes', []):
            identifier = change.get('study_result_id')
            if identifier is None or change.get('passages_only'):
                continue
            for key, synthesis in current.items():
                if isinstance(before.get(key), dict) and identifier in before[key].get('study_result_ids', []) and \
                        identifier in synthesis.get('study_result_ids', []) and before[key] == record(synthesis):
                    found.append({'kind': 'synthesis_uses_changed_value', 'synthesis_id': key, 'study_result_id': identifier,
                                  'edition': edition.get('number'), 'fields': change.get('fields', [])})
    return found


def record(synthesis):
    """What a study table edition keeps of a synthesis."""
    return {k: v for k, v in synthesis.items() if k != 'id'}


def message(issue):
    """A synthesis issue as one line of the mechanical audit."""
    detail = ', '.join(f"{key.replace('_', ' ')} {value}" for key, value in issue.items() if key != 'kind')
    return 'Synthesis: ' + issue['kind'].replace('_', ' ') + (' (' + detail + ')' if detail else '')
