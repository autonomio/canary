"""Methods appendix assembled from the saved review."""
from typing import TypedDict


class Passage(TypedDict):
    source: str
    text: str


class Entry(TypedDict):
    label: str
    text: str
    passages: list[Passage]


class Section(TypedDict):
    title: str
    entries: list[Entry]


class Appendix(TypedDict):
    question: str
    sections: list[Section]


def entry(label, text, passages=()) -> Entry:
    return dict(label=label, text=text, passages=list(passages))


def report(view: dict) -> Appendix:
    graph = view.get('answer', {})
    plan = view.get('query_net', {})
    coverage = view.get('coverage', {})
    selection = view.get('paper_selection', {}).get('selection', {})
    eligibility = plan.get('eligibility', {})
    criteria = [entry(label, eligibility[key]) for key, label in
                [('core', 'Direct evidence'), ('supporting', 'Supporting evidence'), ('exclusion', 'Reasons for exclusion')]
                if eligibility.get(key)]
    criteria.extend(entry('Full-paper check', text) for text in eligibility.get('full_paper_checks', []))
    criteria.extend(entry('Screening instruction', text) for text in plan.get('screening_instructions', []))
    search = []
    progress = coverage.get('search_progress', {})
    for query_id, state in progress.get('queries', {}).items():
        source = state.get('source') or progress.get('adapter') or 'openalex'
        label = source if state.get('pages') or state.get('failure') or state.get('failure_status') else 'Planned query'
        search.append(entry(f'{query_id} · {label}', state.get('search') or 'Search terms were not recorded.'))
    limits = selection.get('selection_limitations') or []
    for limit in [limits] if isinstance(limits, str) else limits:
        search.append(entry('Recorded limitation', limit))
    screening = []
    for decision in selection.get('decisions', []):
        quote = decision.get('supporting_abstract_text')
        screening.append(entry(f"{decision.get('work_id', 'Record')} · {decision.get('decision', 'Decision unknown')}", decision.get('reason', ''),
                               [dict(source='Abstract', text=quote)] if quote else []))
    for row in view.get('access', {}).get('excluded', []):
        screening.append(entry(f"{row.get('work_id', 'Report')} · access", row.get('reason') or row.get('status', '')))
    found, assessed = coverage.get('discovered_candidates'), coverage.get('assessed_candidates')
    if isinstance(found, int) and isinstance(assessed, int) and found >= assessed:
        screening.append(entry('Records left unscreened', str(found - assessed)))
    screening.append(entry('Why screening ended', selection.get('stopping_reason') or
                           coverage.get('stopping_reason') or 'No reason was recorded.'))
    evidence = {e['id']: e for e in graph.get('evidence', []) if e.get('id') and e.get('quote') and not e.get('navigation_id')}
    claims = []
    for claim in graph.get('claims', []):
        passages = [dict(source=evidence[k].get('paragraph_id') or 'Source unknown', text=evidence[k]['quote'])
                    for k in claim.get('evidence_ids', []) if k in evidence]
        claims.append(entry(claim.get('id') or 'Claim', claim.get('text') or 'Claim text was not recorded.', passages))
    return dict(question=graph.get('question') or plan.get('question') or view.get('seed', ''),
                sections=[dict(title='Eligibility', entries=criteria), dict(title='Searches', entries=search),
                          dict(title='Screening and access', entries=screening),
                          dict(title='Claims and supporting passages', entries=claims)])
