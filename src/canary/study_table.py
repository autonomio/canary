"""Studies behind the reviewed reports, bound to the passages that describe them."""
from collections import Counter

SUBJECTS = ('participants', 'specimens', 'experiments', 'datasets', 'other')
MATCHES = ('confirmed', 'uncertain', 'disputed')
CHARACTERISTICS = ('design', 'setting', 'population', 'sample_size', 'follow_up', 'funding')


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _fields(study):
    characteristics = study.get('characteristics') if isinstance(study.get('characteristics'), dict) else {}
    for name in CHARACTERISTICS:
        yield name, characteristics.get(name)
    for group in _rows(study.get('groups')):
        yield 'group ' + str(group.get('id')), group.get('description')
        yield 'group ' + str(group.get('id')) + ' sample size', group.get('sample_size')
    for variable in _rows(study.get('planned_variables')):
        yield 'variable ' + str(variable.get('name')), variable.get('value')


def _cited(owner, name, ids, passages, allowed, outside='undeclared_study_report'):
    if not isinstance(owner, dict):
        owner = {'study_id': owner}
    if not ids:
        yield {'kind': 'lacks_study_passage', **owner, 'field': name}
    for pid in ids:
        passage = passages.get(pid)
        if passage is None:
            yield {'kind': 'missing_study_passage', **owner, 'field': name, 'passage_id': pid}
        elif allowed is not None and passage.get('reference_id') not in allowed:
            yield {'kind': outside, **owner, 'field': name,
                   'passage_id': pid, 'reference_id': passage.get('reference_id')}


def issues(studies, *, references, passages, reviews, plan):
    """Mechanical defects in the recorded studies; scientific judgments stay with the audit."""
    studies = _rows(studies)
    counts = Counter(study.get('id') for study in studies)
    found = [{'kind': 'duplicate_study_id', 'study_id': key} for key, count in counts.items() if count > 1]
    planned = [v for v in plan.get('other_variables', []) if isinstance(v, str)] if isinstance(plan, dict) else []
    members = {}
    for study in studies:
        identifier = study.get('id')
        reports = [r.get('reference_id') for r in _rows(study.get('reports'))]
        for reference in reports:
            members.setdefault(reference, set()).add(identifier)
            if reference not in references:
                found.append({'kind': 'missing_study_report', 'study_id': identifier, 'reference_id': reference})
        for report in _rows(study.get('reports')):
            found.extend(_cited(identifier, 'report ' + str(report.get('reference_id')), report.get('passage_ids', []),
                                passages, [report.get('reference_id')]))
        for name, field in _fields(study):
            if isinstance(field, dict):
                found.extend(_cited(identifier, name, field.get('passage_ids', []), passages, reports))
        groups = Counter(g.get('id') for g in _rows(study.get('groups')))
        found.extend({'kind': 'duplicate_study_group', 'study_id': identifier, 'group_id': key}
                     for key, count in groups.items() if count > 1)
        named = [v.get('name') for v in _rows(study.get('planned_variables'))]
        found.extend({'kind': 'missing_planned_variable', 'study_id': identifier, 'variable': name}
                     for name in planned if name not in named)
        for link in _rows(study.get('shared_with')):
            shared = link.get('study_id')
            if shared == identifier or shared not in counts:
                found.append({'kind': 'unknown_shared_study', 'study_id': identifier, 'shared_study_id': shared})
            found.extend(_cited(identifier, 'shared with ' + str(shared), link.get('passage_ids', []), passages, None))
    for review in reviews:
        reference = review.get('work_id')
        declared = {s for s in review.get('related_study_ids', []) if isinstance(s, str)}
        recorded = {s for s in members.get(reference, set()) if isinstance(s, str)}
        if not recorded:
            found.append({'kind': 'report_without_study', 'reference_id': reference, 'review_study_ids': sorted(declared)})
        elif declared != recorded:
            found.append({'kind': 'study_membership_mismatch', 'reference_id': reference,
                          'review_study_ids': sorted(declared), 'study_ids': sorted(recorded)})
    return found


def _same(left, right, *, exact=False):
    left, right = ' '.join(str(left).split()), ' '.join(str(right).split())
    return left == right if exact else left.casefold() == right.casefold()


def _values(result):
    recorded = result.get('outcome_result') if isinstance(result.get('outcome_result'), dict) else {}
    for name in ('estimate', 'precision'):
        yield name, recorded.get(name)
    for group in _rows(recorded.get('groups')):
        yield 'group ' + str(group.get('group_id')) + ' value', group.get('value')
        yield 'group ' + str(group.get('group_id')) + ' denominator', group.get('denominator')


def _result_fields(result):
    recorded = result.get('outcome_result') if isinstance(result.get('outcome_result'), dict) else {}
    for name in ('time_point', 'effect_measure'):
        yield name, recorded.get(name)
    for name, value in _values(result):
        if not isinstance(value, dict):
            continue
        calculation = value.get('calculation') if isinstance(value.get('calculation'), dict) else {}
        if value.get('origin') != 'calculated' or value.get('passage_ids'):
            yield name, value
        for item in _rows(calculation.get('inputs')):
            yield name + ' input ' + str(item.get('name')), item


def calculation_check(value):
    """Re-run a calculated value's formula; other calculations stay pending verification."""
    if not isinstance(value, dict) or value.get('origin') != 'calculated':
        return None
    from .conversions import Unsupported, evaluate, reproduces
    calculation = value.get('calculation')
    if not isinstance(calculation, dict):
        return {'status': 'pending', 'computed': None, 'reason': 'No formula or inputs were recorded.'}
    inputs = {}
    for item in _rows(calculation.get('inputs')):
        try:
            inputs[item.get('name')] = float(item.get('value'))
        except (TypeError, ValueError):
            return {'status': 'pending', 'computed': None,
                    'reason': f"Input {item.get('name')} is not a plain number: {item.get('value')}"}
    try:
        computed = evaluate(calculation.get('formula'), inputs)
    except Unsupported as error:
        return {'status': 'pending', 'computed': None, 'reason': str(error)}
    if reproduces(value.get('text', ''), computed):
        return {'status': 'reproduced', 'computed': computed, 'reason': None}
    try:
        float(value.get('text'))
    except (TypeError, ValueError):
        return {'status': 'pending', 'computed': computed,
                'reason': f"The recorded value is not a plain number: {value.get('text')}"}
    return {'status': 'not_reproduced', 'computed': computed, 'reason': None}


def result_issues(results, studies, *, versions, passages):
    """Link each study result to its study and to the protocol outcome version it reports.

    versions is None when the protocol cannot be read; outcome versions are then left unchecked.
    """
    found = []
    studies = {study.get('id'): study for study in _rows(studies)}
    recorded_versions = _rows(versions)
    numbers = {version.get('sha256'): version.get('number') for version in recorded_versions}
    outcomes = {version.get('sha256'): {o.get('id'): o for o in _rows(version.get('outcomes'))}
                for version in recorded_versions}
    latest = recorded_versions[-1] if recorded_versions else None
    for result in _rows(results):
        owner = {'study_result_id': result.get('id'), 'study_id': result.get('study_id')}
        declared = result.get('reference_ids', [])
        study = studies.get(result.get('study_id'))
        if study is None:
            found.append({'kind': 'unknown_result_study', **owner})
        else:
            reports = {r.get('reference_id') for r in _rows(study.get('reports'))}
            outside = [r for r in declared if r not in reports]
            if outside:
                found.append({'kind': 'result_outside_study', **owner, 'reference_ids': outside})
        recorded = result.get('outcome_result')
        outcome = result.get('outcome')
        if isinstance(outcome, dict):
            if not isinstance(recorded, dict):
                found.append({'kind': 'missing_outcome_result', **owner, 'outcome_id': outcome.get('id')})
            if versions is not None:
                cited = outcome.get('protocol_version')
                definition = outcomes.get(cited, {}).get(outcome.get('id'))
                if cited not in outcomes:
                    found.append({'kind': 'unknown_protocol_version', **owner, 'protocol_version': cited})
                elif definition is None:
                    found.append({'kind': 'unknown_protocol_outcome', **owner, 'outcome_id': outcome.get('id')})
                if cited in outcomes and cited != latest.get('sha256'):
                    found.append({'kind': 'superseded_protocol_version', **owner, 'protocol_version': cited,
                                  'version_number': numbers[cited], 'latest_version_number': latest.get('number')})
                if isinstance(recorded, dict) and definition is not None:
                    units = (result.get('fields') or {}).get('units')
                    for name, field in (('unit', units), ('time_point', recorded.get('time_point')),
                                        ('effect_measure', recorded.get('effect_measure'))):
                        value = field.get('text') if isinstance(field, dict) else None
                        if definition.get(name) is not None and (
                                value is None or not _same(value, definition[name], exact=name == 'unit')):
                            found.append({'kind': 'outcome_definition_mismatch', **owner, 'field': name,
                                          'protocol_value': definition[name], 'recorded_value': value})
        if isinstance(recorded, dict):
            favours = recorded.get('larger_value_favours')
            if isinstance(favours, dict):
                found.extend(_cited(owner, 'larger value favours', favours.get('passage_ids', []), passages,
                                    declared, 'undeclared_result_reference'))
            if study is not None:
                groups = {g.get('id') for g in _rows(study.get('groups'))}
                named = [g.get('group_id') for g in _rows(recorded.get('groups'))]
                if isinstance(favours, dict) and favours.get('group_id') is not None:
                    named.append(favours.get('group_id'))
                found.extend({'kind': 'unknown_result_group', **owner, 'group_id': g}
                             for g in dict.fromkeys(named) if g not in groups)
        for name, field in _result_fields(result):
            if isinstance(field, dict):
                found.extend(_cited(owner, name, field.get('passage_ids', []), passages, declared,
                                    'undeclared_result_reference'))
        for name, value in _values(result):
            check = calculation_check(value)
            if check is not None and value.get('calculation') is None:
                found.append({'kind': 'calculation_missing', **owner, 'field': name})
            elif check is not None and check['status'] == 'not_reproduced':
                found.append({'kind': 'calculation_not_reproduced', **owner, 'field': name,
                              'recorded_value': value.get('text'), 'computed_value': check['computed']})
    return found


def export_result(result):
    """Name outcome passage links the way exported result fields do."""
    recorded = result.get('outcome_result')
    if not isinstance(recorded, dict):
        return {}
    def bound(field):
        if not isinstance(field, dict):
            return None
        exported = {'value': field.get('text'), 'paragraph_ids': list(field.get('passage_ids', []))}
        if 'origin' in field:
            calculation = field.get('calculation')
            exported.update(origin=field.get('origin'), calculation_check=calculation_check(field), calculation=dict(
                calculation, inputs=[dict(i, paragraph_ids=list(i.get('passage_ids', []))) for i in _rows(calculation.get('inputs'))])
                if isinstance(calculation, dict) else None)
        return exported
    favours = recorded.get('larger_value_favours')
    return {'outcome_result': dict(recorded, **{name: bound(recorded.get(name)) for name in
                                                ('time_point', 'effect_measure', 'estimate', 'precision')},
                                   groups=[dict(g, value=bound(g.get('value')), denominator=bound(g.get('denominator')))
                                           for g in _rows(recorded.get('groups'))],
                                   larger_value_favours=dict(favours, paragraph_ids=list(favours.get('passage_ids', [])))
                                   if isinstance(favours, dict) else None)}


def result_passage_ids(results):
    for result in _rows(results):
        for _, field in _result_fields(result):
            if isinstance(field, dict):
                yield from field.get('passage_ids', [])
        favours = (result.get('outcome_result') or {}).get('larger_value_favours') if isinstance(result.get('outcome_result'), dict) else None
        if isinstance(favours, dict):
            yield from favours.get('passage_ids', [])


def passage_ids(studies):
    for study in _rows(studies):
        for report in _rows(study.get('reports')):
            yield from report.get('passage_ids', [])
        for link in _rows(study.get('shared_with')):
            yield from link.get('passage_ids', [])
        for _, field in _fields(study):
            if isinstance(field, dict):
                yield from field.get('passage_ids', [])


def summary(studies):
    """Count studies and reports; a subject total stays unknown unless each subject is counted once."""
    studies = _rows(studies)
    kinds = {study.get('id'): study.get('subject') for study in studies}
    repeated = {key for key, count in Counter(study.get('id') for study in studies).items() if count > 1}
    linked = {}
    for study in studies:
        for link in _rows(study.get('shared_with')):
            if link.get('study_id') != study.get('id'):
                pair = tuple(sorted((study.get('id'), link.get('study_id')), key=str))
                linked.setdefault(pair, set()).add(str(link.get('shares')))
    subjects = []
    for subject in dict.fromkeys(study.get('subject') for study in studies):
        members = [study for study in studies if study.get('subject') == subject]
        reasons = [f"Study {study.get('id')} does not report its number of {subject}."
                   for study in members if type(study.get('subject_count')) is not int]
        reasons.extend(f"Study {key} is recorded more than once." for key in sorted(repeated, key=str)
                       if kinds.get(key) == subject)
        reasons.extend(f"Studies {a} and {b} share {' and '.join(sorted(shared))}, so their counts of {subject} may overlap."
                       for (a, b), shared in sorted(linked.items(), key=str)
                       if kinds.get(a) == subject and kinds.get(b) == subject)
        subjects.append({'subject': subject, 'studies': len(members), 'reasons': reasons,
                         'total': None if reasons else sum(study['subject_count'] for study in members)})
    reports = {r.get('reference_id') for study in studies for r in _rows(study.get('reports'))}
    return {'studies': len(studies), 'reports': len(reports), 'subjects': subjects}


def message(issue):
    detail = ', '.join(f"{key.replace('_', ' ')} {value}" for key, value in issue.items() if key != 'kind')
    return 'Study table: ' + issue['kind'].replace('_', ' ') + (' (' + detail + ')' if detail else '')


def export(studies):
    """Name passage links the way exported study results do."""
    def bound(field):
        return {'value': field.get('text'), 'paragraph_ids': list(field.get('passage_ids', []))} if isinstance(field, dict) else None
    exported = []
    for study in _rows(studies):
        characteristics = study.get('characteristics') if isinstance(study.get('characteristics'), dict) else {}
        exported.append(dict(study, characteristics={name: bound(characteristics.get(name)) for name in CHARACTERISTICS},
                             groups=[dict(g, description=bound(g.get('description')), sample_size=bound(g.get('sample_size')))
                                     for g in _rows(study.get('groups'))],
                             planned_variables=[dict(v, value=bound(v.get('value'))) for v in _rows(study.get('planned_variables'))]))
    return exported
