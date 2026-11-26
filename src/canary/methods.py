"""Methods appendix assembled from the saved review."""
import json
from pathlib import Path
from typing import TypedDict

from .methods_automation import ROLES, sessions
from .methods_search import searches, working_file


class Passage(TypedDict):
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


class Appendix(TypedDict):
    question: str
    searches: list[dict]
    sections: list[Section]


def entry(label, text, passages=()) -> Entry:
    return dict(label=label, text=text, passages=list(passages), url=None)


def rows(values):
    return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def strings(values):
    if isinstance(values, str):
        return [values]
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


def criteria_entries(plan):
    eligibility = plan.get('eligibility') or {}
    criteria = [entry(label, eligibility[key]) for key, label in
                [('core', 'Direct evidence'), ('supporting', 'Supporting evidence'), ('exclusion', 'Reasons for exclusion')]
                if eligibility.get(key)]
    criteria.extend(entry('Full-paper check', text) for text in strings(eligibility.get('full_paper_checks')))
    criteria.extend(entry('Screening instruction', text) for text in strings(plan.get('screening_instructions')))
    return criteria


def search_entries(query_rows, selection):
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


def screening_entries(view, selection, coverage):
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
        screening.append(entry(f"{row.get('work_id', 'Report')} · full-paper review",
                               (row.get('eligibility') or 'Eligibility unknown') + '\n' +
                               (row.get('substantive_review_note') or '')))
    found, assessed = coverage.get('discovered_candidates'), coverage.get('assessed_candidates')
    if type(found) is int and type(assessed) is int and found >= assessed >= 0:
        screening.append(entry('Records left unscreened', str(found - assessed)))
    screening.append(entry('Why screening ended', selection.get('stopping_reason') or
                           coverage.get('stopping_reason') or 'No reason was recorded.'))
    return screening


def claim_entries(graph):
    evidence = {e['id']: e for e in rows(graph.get('evidence')) if e.get('id') and e.get('quote') and not e.get('navigation_id')}
    claims = []
    for claim in rows(graph.get('claims')):
        passages = [dict(source=evidence[k].get('paragraph_id') or 'Source unknown', text=evidence[k]['quote'])
                    for k in strings(claim.get('evidence_ids')) if k in evidence]
        missing = any(k not in evidence for k in strings(claim.get('evidence_ids')))
        text = claim.get('text') or 'Claim text was not recorded.'
        if missing:
            text += '\nThe record does not identify a unique passage for every citation.'
        claims.append(entry(claim.get('id') or 'Claim', text, passages))
    return claims


def automation_entries(view, selection, workspace):
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


def report(view: dict, workspace: Path | None) -> Appendix:
    graph = view.get('answer') or {}
    plan = view.get('query_net') or {}
    coverage = view.get('coverage') or {}
    selection = (view.get('paper_selection') or {}).get('selection') or {}
    progress = coverage.get('search_progress') or {}
    for name, target in [('question-plan.json', plan), ('search-progress.json', progress), ('selection.json', selection)]:
        if not target:
            path = working_file(workspace, name)
            if path is not None:
                saved = json.loads(path.read_text())
                if isinstance(saved, dict):
                    target.update(saved)
    warnings = []
    query_rows = searches(workspace, progress, plan.get('queries') or [], warnings=warnings)
    search = search_entries(query_rows, selection)
    search.extend(entry('Incomplete request log', warning) for warning in warnings)
    return dict(question=graph.get('question') or plan.get('question') or view.get('seed') or '',
                searches=query_rows,
                sections=[dict(title='Eligibility', entries=criteria_entries(plan)),
                          dict(title='Searches', entries=search),
                          dict(title='Screening and access', entries=screening_entries(view, selection, coverage)),
                          dict(title='Automation and checks', entries=automation_entries(view, selection, workspace)),
                          dict(title='Claims and supporting passages', entries=claim_entries(graph))])


def for_bundle(workspace, manifest, store=None):
    """Read a finished bundle while the caller holds its read lock."""
    from . import compact_bundle, dashboard, review_history
    if compact_bundle.exists(workspace):
        checksum = (workspace / compact_bundle.CHECKSUM).read_text().strip()
        saved = review_history.retained_methods(workspace, manifest['id'], checksum, store)
        if saved is not None:
            return saved
        result = report(compact_bundle.projection(workspace), None)
        result['sections'].insert(0, dict(title='Limits of the saved record', entries=[
            entry('Older bundle', 'Detailed search receipts, access decisions and session prompts were not retained.')]))
        return result
    return report(dashboard.research_bundle(workspace), workspace)
