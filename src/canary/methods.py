"""Methods appendix assembled from the saved review."""
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TypedDict

from .methods_automation import ROLES, sessions
from .methods_search import searches, working_file


class _PassageWarning(TypedDict, total=False):
    warning: str


class Passage(_PassageWarning):
    source: str
    text: str


class Entry(TypedDict):
    label: str
    text: str
    passages: list[Passage]
    url: str | None


class Section(TypedDict):
    title: str
    entries: list[Entry]


class _ProtocolRecord(TypedDict, total=False):
    protocol_versions: list[dict]


class Appendix(_ProtocolRecord):
    question: str
    searches: list[dict]
    sections: list[Section]


def entry(label: str, text: str, passages: Iterable[Passage] = ()) -> Entry:
    """Build a labelled appendix entry with its supporting passages."""
    return dict(label=label, text=text, passages=list(passages), url=None)


def rows(values: object) -> list[dict]:
    """Read object rows from an optional saved list."""
    return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def strings(values: object) -> list[str]:
    """Read text saved as a single string or a list."""
    if isinstance(values, str):
        return [values]
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


def criteria_entries(plan: dict) -> list[Entry]:
    """Describe the eligibility rules and screening instructions."""
    eligibility = plan.get('eligibility') or {}
    criteria = [entry(label, eligibility[key]) for key, label in
                [('core', 'Direct evidence'), ('supporting', 'Supporting evidence'), ('exclusion', 'Reasons for exclusion')]
                if eligibility.get(key)]
    criteria.extend(entry('Full-paper check', text) for text in strings(eligibility.get('full_paper_checks')))
    criteria.extend(entry('Screening instruction', text) for text in strings(plan.get('screening_instructions')))
    return criteria


def search_entries(query_rows: list[dict], selection: dict) -> list[Entry]:
    """Describe each search round and its recorded limits."""
    search = []
    for row in query_rows:
        source = row['source'] if row['attempted'] else 'Planned query'
        number = row['round']
        label = f"{row['query_id']} · {source} · " + (f"Round {number}" if number else 'Round unknown')
        details = [row['search']]
        for key, name in [('matched', 'Matches reported by the source'), ('retrieved', 'Records retrieved'),
                          ('last_search', 'Last search')]:
            value = row[key]
            if key == 'retrieved' and row['retrieved_partial'] and value is not None:
                value = f'at least {value}'
            details.append(f"{name}: {value if value is not None else 'not recorded'}")
        details.append(row['ended'] or 'No stopping reason was recorded.')
        search.append(entry(label, '\n'.join(details)))
    for limit in strings(selection.get('selection_limitations')):
        search.append(entry('Recorded limitation', limit))
    return search


def screening_entries(view: dict, selection: dict, coverage: dict) -> list[Entry]:
    """Summarise recorded decisions and account for unscreened records."""
    screening = []
    for decision in rows(selection.get('decisions')):
        quote = decision.get('supporting_abstract_text')
        disposition = decision.get('decision') or 'Decision unknown'
        reason = str(decision.get('reason') or '')
        if disposition == 'exclude' and decision.get('decided_by') == 'software_only':
            reason = (reason + ' Software made this decision alone.').strip()
        screening.append(entry(f"{decision.get('work_id', 'Record')} · {disposition}", reason,
                               [dict(source='Abstract', text=quote)] if quote else []))
    for row in rows((view.get('access') or {}).get('excluded')):
        screening.append(entry(f"{row.get('work_id', 'Report')} · access", row.get('reason') or row.get('status', '')))
    ledger = view.get('paper_selection') or {}
    for row in rows(ledger.get('reviewed_papers')):
        eligibility = row.get('eligibility') or 'Eligibility unknown'
        details = [str(value) for value in (eligibility, row.get('reason'), row.get('substantive_review_note')) if value]
        screening.append(entry(f"{row.get('work_id', 'Report')} · full-paper review", '\n'.join(dict.fromkeys(details))))
    found, assessed = coverage.get('discovered_candidates'), coverage.get('assessed_candidates')
    if type(found) is int and type(assessed) is int and found >= assessed >= 0:
        screening.append(entry('Records left unscreened', str(found - assessed)))
    screening.append(entry('Why screening ended', selection.get('stopping_reason') or
                           coverage.get('stopping_reason') or 'No reason was recorded.'))
    return screening


def claim_entries(graph: dict) -> list[Entry]:
    """Pair claims with the passages cited in the saved answer."""
    evidence = {e['id']: e for e in rows(graph.get('evidence')) if e.get('id') and e.get('quote') and not e.get('navigation_id')}
    claims = []
    for claim in rows(graph.get('claims')):
        passages: list[Passage] = []
        for key in strings(claim.get('evidence_ids')):
            if key in evidence:
                record = evidence[key]
                passage = Passage(source=record.get('paragraph_id') or 'Source unknown', text=record['quote'])
                if record.get('anchored') is False:
                    passage['warning'] = 'Canary could not verify this quotation against the saved source text.'
                passages.append(passage)
        missing = any(k not in evidence for k in strings(claim.get('evidence_ids')))
        text = claim.get('text') or 'Claim text was not recorded.'
        if missing:
            text += '\nThe record does not identify a unique passage for every citation.'
        claims.append(entry(claim.get('id') or 'Claim', text, passages))
    return claims


def automation_entries(view: dict, selection: dict, workspace: Path | None) -> list[Entry]:
    """Describe the software and checks recorded for each review step."""
    automation = []
    recorded = sessions(workspace) if workspace is not None else []
    run = view.get('run') or {}
    for role in ('research', 'audit'):
        if run.get(role + '_model'):
            automation.append(entry(f'Configured {role} settings', run[role + '_model'] +
                                    ' · reasoning effort ' + (run.get(role + '_effort') or 'not recorded')))

    for session in recorded:
        config = session['configuration']
        name = ROLES[config['role']]
        if session['round'] is not None:
            name += f" · Round {int(session['round'])}"
        details = [f"{label}: {config[key] or 'not recorded'}" for key, label in
                   [('tool', 'Tool'), ('version', 'Version'), ('model', 'Model'), ('effort', 'Reasoning effort'),
                    ('prompt_sha256', 'Prompt SHA-256')]]
        if session['exit_code'] is not None:
            details.append('Session completed.' if session['exit_code'] == 0 else 'Session stopped with an error.')
        details.append('No separate check of this step was recorded.')
        if session['follow_up']:
            details.append('The receipt records a retry or capacity pause. The saved prompt covers only the initial instructions.')
        details.append('No matching evaluation was recorded.' if not session['evaluation'] else 'Matching evaluation available.')
        passages = [dict(source='Saved prompt', text=session['prompt'])] if session['prompt'] else []
        record = entry(name, '\n'.join(details), passages)
        record['url'] = session['evaluation']
        automation.append(record)
    audit = (view.get('answer') or {}).get('audit') or {}
    if audit:
        status = audit.get('status') or 'unknown'
        detail = f"Audit status: {status}."
        if status in ('complete', 'partial') and type(audit.get('checked_claims')) is int:
            detail += f" {audit.get('model') or 'Reviewer unknown'} checked {audit['checked_claims']} claims."
        automation.append(entry('Who checked the findings', detail))
    if not recorded:
        automation.append(entry('Software used', 'No session receipt was saved.'))
    translation = selection.get('translation_method')
    automation.append(entry('Translation', translation or 'No translation method was recorded.'))
    return automation


def _protocol_text(value: object) -> str:
    if isinstance(value, dict):
        return ' · '.join(f"{key.replace('_', ' ')}: {_protocol_text(part)}" for key, part in value.items()) or '{}'
    if isinstance(value, list):
        return ', '.join(_protocol_text(part) for part in value) or '[]'
    return str(value) if value is not None else 'Unspecified'


def _protocol_changes(label: str, before: object, after: object) -> list[Entry]:
    if before == after:
        return []
    changes = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in dict.fromkeys([*before, *after]):
            field = (label + ' · ' if label else '') + key.replace('_', ' ').capitalize()
            if key not in before:
                changes.append(entry('Added · ' + field, _protocol_text(after[key])))
            elif key not in after:
                changes.append(entry('Removed · ' + field, _protocol_text(before[key])))
            else:
                changes.extend(_protocol_changes(field, before[key], after[key]))
    elif isinstance(before, list) and isinstance(after, list):
        remaining = list(after)
        for item in before:
            if item in remaining:
                remaining.remove(item)
            else:
                changes.append(entry('Removed · ' + label, _protocol_text(item)))
        changes.extend(entry('Added · ' + label, _protocol_text(item)) for item in remaining)
        if not changes:
            changes.append(entry('New order · ' + label, _protocol_text(after)))
    else:
        changes.extend([entry('Previously · ' + label, _protocol_text(before)),
                        entry('Now · ' + label, _protocol_text(after))])
    return changes


def _query_text(query: dict) -> str:
    details = [f"{k.replace('_', ' ').capitalize()}: {_protocol_text(v)}"
               for k, v in query.items() if k not in ('id', 'search') and v]
    return '\n'.join([query['search'], *details])


def _identified_queries(value: object) -> bool:
    return (isinstance(value, list) and all(isinstance(q, dict) and isinstance(q.get('id'), str)
            and isinstance(q.get('search'), str) for q in value) and len({q['id'] for q in value}) == len(value))


def _amendment(previous: dict, version: dict) -> Section:
    entries = []
    old, new = previous['plan'], version['plan']
    for key in dict.fromkeys([*old, *new]):
        if key == 'draft_author':
            continue
        if key == 'queries' and _identified_queries(old.get(key)) and _identified_queries(new.get(key)):
            before = {q['id']: q for q in old[key]}
            after = {q['id']: q for q in new[key]}
            for identifier in dict.fromkeys([*before, *after]):
                if identifier not in before:
                    entries.append(entry('Added search · ' + identifier, _query_text(after[identifier])))
                elif identifier not in after:
                    entries.append(entry('Removed search · ' + identifier, _query_text(before[identifier])))
                else:
                    entries.extend(_protocol_changes(identifier, before[identifier], after[identifier]))
            common = before.keys() & after.keys()
            if [i for i in before if i in common] != [i for i in after if i in common]:
                entries.append(entry('New search order', ', '.join(after)))
        else:
            entries.extend(_protocol_changes('', {key: old[key]} if key in old else {},
                                             {key: new[key]} if key in new else {}))
    entries.extend([entry('Reason', version['reason'] or 'Not recorded'),
                    entry('Timing', version['stage']), entry('Recorded by Canary', version['recorded_at'])])
    return dict(title=f"Protocol amendment {version['number'] - 1}", entries=entries)


def report(view: dict, workspace: Path | None) -> Appendix:
    """Assemble the appendix from the projection and available working records."""
    graph = view.get('answer') or {}
    plan = view.get('query_net') or {}
    coverage = view.get('coverage') or {}
    selection = (view.get('paper_selection') or {}).get('selection') or {}
    progress = coverage.get('search_progress') or {}
    missing = []
    for name, target in [('question-plan.json', plan), ('search-progress.json', progress), ('selection.json', selection)]:
        if not target:
            path = working_file(workspace, name)
            if path is not None:
                try:
                    saved = json.loads(path.read_bytes())
                except (ValueError, OSError):
                    saved = None
                if isinstance(saved, dict):
                    target.update(saved)
                else:
                    missing.append(entry(name, f'Canary could not read the saved {name} record.'))
    from .review_protocol import FILE, versions as read_versions, outcome_definitions
    history = working_file(workspace, FILE)
    versions = read_versions(history) if history else []
    protocol_sections = []
    for index, version in enumerate(versions):
        if index:
            protocol_sections.append(_amendment(versions[index - 1], version))
            continue
        protocol_entries = []
        saved_plan = version['plan']
        title = 'Original protocol' if version['original'] else 'Earliest retained protocol'
        protocol_entries.extend([entry('Question', saved_plan['question']),
                                 entry('Recorded by Canary', version['recorded_at']),
                                 entry('Timing', version['stage'])])
        if version['author'] != 'Not recorded' and 'draft_author' not in saved_plan:
            protocol_entries.append(entry('Protocol author', version['author']))
        for field, label in [('rationale', 'Rationale'), ('other_variables', 'Other variable'),
                             ('study_groups', 'Study group'), ('result_selection', 'Choosing results'),
                             ('missing_information', 'Missing information')]:
            protocol_entries.extend(entry(label, text) for text in strings(saved_plan.get(field)))
        for outcome in version.get('outcomes') or outcome_definitions(saved_plan.get('outcomes', [])):
            details = [f"{label}: {outcome[field]}" for field, label in
                       [('unit', 'Unit'), ('time_point', 'Time point'), ('effect_measure', 'Effect measure')]
                       if outcome[field] is not None]
            protocol_entries.append(entry('Outcome', ' · '.join([outcome['name'], *details])))
        protocol_entries.extend(criteria_entries(saved_plan))
        protocol_sections.append(dict(title=title, entries=protocol_entries))
    if versions:
        spec_path = working_file(workspace, 'run-spec.json')
        spec = json.loads(spec_path.read_bytes()) if spec_path else {}
        registration = spec.get('registration', {})
        status = registration.get('status')
        if status == 'registered':
            details = [entry('Registry', registration['registry']), entry('Identifier', registration['identifier'])]
            if registration.get('registered_on'):
                details.append(entry('Date reported in the registry', registration['registered_on']))
            if registration.get('record_url'):
                details.append(entry('Registry record supplied by the author', registration['record_url']))
        else:
            details = [entry('Registration', 'Declared unregistered.' if status == 'unregistered'
                             else 'Registration status was not recorded.')]
        protocol_sections.append(dict(title='Protocol registration', entries=details))
    warnings = []
    query_rows = searches(workspace, progress, plan.get('queries') or [], warnings=warnings)
    search = search_entries(query_rows, selection)
    search.extend(entry('Incomplete request log', warning) for warning in warnings)
    return dict(question=graph.get('question') or plan.get('question') or view.get('seed') or '',
                protocol_versions=versions, searches=query_rows,
                sections=([dict(title='Limits of the saved record', entries=missing)] if missing else []) +
                         protocol_sections +
                         [dict(title='Eligibility', entries=criteria_entries(plan)),
                          dict(title='Searches', entries=search),
                          dict(title='Screening and access', entries=screening_entries(view, selection, coverage)),
                          dict(title='Automation and checks', entries=automation_entries(view, selection, workspace)),
                          dict(title='Claims and supporting passages', entries=claim_entries(graph))])


def for_bundle(workspace: Path, manifest: dict, store: Path | None = None, *, display: bool = False) -> Appendix:
    """Read a finished bundle while the caller holds its read lock."""
    from . import compact_bundle, dashboard, review_history
    if compact_bundle.exists(workspace):
        checksum = (workspace / compact_bundle.CHECKSUM).read_text().strip()
        saved = review_history.retained_methods(workspace, manifest['id'], checksum, store)
        if saved is not None:
            if not display:
                return saved
            versions = saved.get('protocol_versions', [])
            sections = (_amendment(previous, version) for previous, version in zip(versions, versions[1:])
                        if isinstance(previous, dict) and isinstance(previous.get('plan'), dict)
                        and isinstance(version, dict) and isinstance(version.get('plan'), dict)
                        and isinstance(version.get('number'), int)
                        and {'stage', 'reason', 'recorded_at'} <= version.keys())
            amendments = {section['title']: section for section in sections}
            sections = []
            for section in saved['sections']:
                if section['title'] in ('Original protocol', 'Earliest retained protocol'):
                    section = dict(section, entries=[e for e in section['entries'] if e['label'] != 'Drafted by'])
                sections.append(amendments.get(section['title'], section))
            return dict(saved, sections=sections)
        result = report(compact_bundle.projection(workspace), None)
        result['sections'].insert(0, dict(title='Limits of the saved record', entries=[
            entry('Older bundle', 'Detailed search receipts, access decisions and session prompts were not retained.')]))
        return result
    return report(dashboard.research_bundle(workspace), workspace)
