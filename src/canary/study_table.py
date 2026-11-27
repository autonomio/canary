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


def _cited(identifier, name, ids, passages, allowed):
    if not ids:
        yield {'kind': 'lacks_study_passage', 'study_id': identifier, 'field': name}
    for pid in ids:
        passage = passages.get(pid)
        if passage is None:
            yield {'kind': 'missing_study_passage', 'study_id': identifier, 'field': name, 'passage_id': pid}
        elif allowed is not None and passage.get('reference_id') not in allowed:
            yield {'kind': 'undeclared_study_report', 'study_id': identifier, 'field': name,
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
