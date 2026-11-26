"""PRISMA 2020 reporting checklist for saved reviews."""
from functools import lru_cache
from html import escape
from typing import Literal, TypedDict
from urllib.parse import quote
from importlib.resources import files
import json


@lru_cache(maxsize=1)
def catalogue() -> dict:
    return json.loads(files('canary').joinpath('prisma_2020.json').read_text(encoding='utf-8'))


Status = Literal['complete', 'partial', 'missing', 'inapplicable']


class Passage(TypedDict):
    text: str
    evidence: list[str]
    supplier: Literal['run', 'author']


class Element(TypedDict):
    basis: str
    requirement: str
    status: Status
    location: str | None
    passages: list[Passage]


class Item(TypedDict):
    reader_question: str
    heading: str
    id: str
    title: str
    status: Status
    elements: list[Element]
    missing_elements: list[str]
    see_also: list[str]


class Count(TypedDict):
    label: str
    value: int | None
    evidence: str | None


class Report(TypedDict):
    selection_summary: list[str]
    counts: list[Count]
    version: str
    citation: str
    license: str
    sources: list[str]
    question: str
    edition: str
    items: list[Item]


def counts(coverage: dict) -> list[Count]:
    result = []
    for label, field in [('Unique candidate records', 'discovered_candidates'),
                         ('Included reports', 'included_report_count'),
                         ('Reviewed publications', 'reviewed_papers'),
                         ('Included studies', 'included_study_count'),
                         ('Participants', 'participant_count')]:
        value = coverage.get(field)
        known = type(value) is int and value >= 0
        result.append(Count(label=label, value=value if known else None, evidence='/coverage/'+field if known else None))
    return result


def count_text(count: Count) -> str:
    value = count['value']
    return f"{count['label']}: {value:,}" if value is not None else count['label'] + ': unknown'


def selection_summary(view: dict) -> list[str]:
    coverage = view.get('coverage', {})
    selection = view.get('paper_selection', {}).get('selection', {})
    lines = []
    if selection.get('method') == 'coverage':
        lines.append('Canary chose papers to cover different parts of the question.')
    total, assessed = coverage.get('discovered_candidates'), coverage.get('assessed_candidates')
    if type(total) is int and type(assessed) is int and 0 <= assessed <= total:
        lines.append(f'Canary assessed {assessed:,} of the {total:,} records it found.')
        if total > assessed:
            lines.append(f'The remaining {total - assessed:,} records may include relevant papers.')
    queries = (coverage.get('search_progress') or {}).get('queries', {})
    if isinstance(queries, dict) and queries and all(isinstance(q, dict) for q in queries.values()):
        capped = sum(q.get('truncated') is True for q in queries.values())
        if capped:
            lines.append(f'{capped} of {len(queries)} searches stopped before reaching the last page of results.')
    selected = selection.get('selected_for_review_ids')
    if isinstance(selected, list) and all(isinstance(value, str) and value for value in selected):
        lines.append(f'Canary selected {len(set(selected)):,} records for full review.')
    available = coverage.get('available_papers')
    if type(available) is int and available >= 0:
        lines.append(f'The review had access to {available:,} full papers.')
    return lines


def _run_answers(view: dict) -> dict:
    answers = {}
    def add(key, text, path, status='partial'):
        if isinstance(text, str) and text.strip():
            answers[key] = (status, [dict(text=text.strip(), evidence=[path], supplier='run')])

    graph = view.get('answer', {})
    question = graph.get('question')
    if question == '?':
        question = None
    add('4:1', question, '/answer/question')
    add('abstract-2:1', question, '/answer/question', 'complete')
    plan = view.get('query_net', {})
    selection = view.get('paper_selection', {}).get('selection', {})
    add('8:4', selection.get('approach'), '/paper_selection/selection/approach')
    add('6:2', view.get('run', {}).get('search_scope'), '/run/search_scope')
    eligibility = plan.get('eligibility')
    if isinstance(eligibility, dict):
        text = '\n'.join(f'{role}: {rule}' for role, rule in eligibility.items() if isinstance(rule, str) and rule.strip())
        add('5:1', text, '/query_net/eligibility')
        add('abstract-3:1', text, '/query_net/eligibility')
    progress = view.get('coverage', {}).get('search_progress') or {}
    queries = progress.get('queries', {})
    if isinstance(queries, dict):
        lines = [q['search'] for q in queries.values() if isinstance(q, dict) and isinstance(q.get('search'), str)]
        add('7:1', '\n'.join(lines), '/coverage/search_progress/queries')
    summaries = [row['text'] for row in graph.get('executive_summary', [])
                 if isinstance(row, dict) and isinstance(row.get('text'), str)]
    add('abstract-8:1', '\n'.join(summaries), '/answer/executive_summary')
    outcomes = plan.get('outcomes', [])
    if isinstance(outcomes, list):
        add('10a:1', '\n'.join(o for o in outcomes if isinstance(o, str)), '/query_net/outcomes')
    limitations = selection.get('selection_limitations')
    if isinstance(limitations, list):
        limitations = '\n'.join(text for text in limitations if isinstance(text, str) and text.strip())
    add('23c:1', limitations, '/paper_selection/selection/selection_limitations')
    next_questions = [q['question'] for q in graph.get('next_questions', [])
                      if isinstance(q, dict) and isinstance(q.get('question'), str)]
    add('23d:2', '\n'.join(next_questions), '/answer/next_questions')
    for index, result in enumerate(graph.get('study_results', [])):
        fields = result.get('fields', {}) if isinstance(result, dict) else {}
        for key, field in [('17:2', 'setup'), ('19:1', 'value')]:
            value = fields.get(field)
            if isinstance(value, dict) and isinstance(value.get('value'), str) and value['value'].strip():
                path = f'/answer/study_results/{index}/fields/{field}/value'
                _, passages = answers.setdefault(key, ('partial', []))
                passages.append(dict(text=value['value'], evidence=[path, *value.get('paragraph_ids', [])], supplier='run'))
    for key, field in [('abstract-7:1', 'included_study_count'), ('abstract-7:2', 'participant_count')]:
        total = view.get('coverage', {}).get(field)
        if type(total) is int and total >= 0:
            add(key, str(total), '/coverage/'+field, 'complete')
    return answers


def _status(statuses: list[Status]) -> Status:
    if all(s == 'inapplicable' for s in statuses):
        return 'inapplicable'
    if all(s in ('complete', 'inapplicable') for s in statuses):
        return 'complete'
    return 'partial' if any(s in ('complete', 'partial') for s in statuses) else 'missing'


def report(view: dict, edition: dict) -> Report:
    data = catalogue()
    answers = _run_answers(view)
    declarations = _declarations(edition['entries'])
    items: list[Item] = []
    for definition in data['items']:
        number = definition['id']
        elements: list[Element] = []
        for i, requirement in enumerate(definition['elements'], 1):
            key = f'{number}:{i}'
            status, passages = answers.get(key, ('missing', []))
            basis = 'saved_run' if passages else 'not_recorded'
            if key in declarations:
                statements = declarations[key]
                passages = [*passages, *(p for _, p in statements)]
                basis = statements[0][0] if len(statements) == 1 else 'unresolved_decisions'
                if basis == 'reported' or (basis == 'not_used' and number in ('24a', '24b')):
                    status = 'complete'
                elif basis == 'inapplicable' or (basis == 'not_used' and requirement.startswith('If ')):
                    status = 'inapplicable'
                elif basis == 'archive_missing':
                    status = 'missing'
                else:
                    status = 'partial'
            location = f'#answer-{number}-{i}' if passages else None
            elements.append(dict(requirement=requirement, basis=basis, status=status, location=location, passages=passages))
        items.append(dict(reader_question=definition['reader_question'], id=number, title=definition['title'], heading=data['headings'][definition['heading']],
                          status=_status([e['status'] for e in elements]),
                          elements=elements, see_also=definition.get('see_also', []),
                          missing_elements=[e['requirement'] for e in elements if e['status'] in ('partial', 'missing')]))
    abstract = [row for row in items if row['id'].startswith('abstract-')]
    abstract_item = next(row for row in items if row['id'] == '2')
    abstract_element = abstract_item['elements'][0]
    abstract_status = _status([row['status'] for row in abstract])
    abstract_item['status'] = abstract_element['status'] = abstract_status
    abstract_item['missing_elements'] = [row['id'] + ' · ' + row['title'] for row in abstract
                                        if row['status'] in ('partial', 'missing')]
    abstract_element['basis'] = 'abstract_checklist'
    abstract_element['location'] = '#answer-2-1'
    abstract_element['passages'] = [Passage(text='See the twelve abstract items below.',
                                            evidence=abstract_item['see_also'], supplier='run')]
    result = Report(selection_summary=selection_summary(view), counts=counts(view.get('coverage', {})), version=data['version'], citation=data['citation'], license=data['license'], sources=data['sources'],
                question=view.get('answer', {}).get('question') or 'Research review', edition=edition['last_entry'], items=items)
    validate(result)
    return result


def _declarations(entries: list[dict]) -> dict[str, list[tuple[str, Passage]]]:
    declarations: dict[str, list[tuple[str, Passage]]] = {}
    replaced = {entry['replaces'] for entry in entries if entry.get('replaces')}
    for entry in entries:
        d = entry['decision']
        subject = entry.get('subject_id') or d.get('subject_id', '')
        if (entry['id'] in replaced or not subject.startswith('prisma:') or
                d['status'] not in ('accepted', 'modified') or (d.get('decided_by') or {}).get('kind') != 'person' or
                d['judgement'] not in ('reported', 'partial', 'not_used', 'archive_missing', 'inapplicable')):
            continue
        if not d['reason'].strip() or not any(str(e).strip() for e in d['evidence']):
            continue
        passage = Passage(text=d['reason'], evidence=[entry['id'], *d['evidence']], supplier='author')
        declarations.setdefault(subject.removeprefix('prisma:'), []).append((d['judgement'], passage))
    return declarations


def validate(result: Report) -> None:
    """Check exported rows and their answer targets against the published catalogue."""
    expected = {r['id']: r for r in catalogue()['items']}
    ids = [r['id'] for r in result['items']]
    if len(ids) != len(expected) or set(ids) != set(expected):
        raise ValueError('Missing, duplicate or unexpected PRISMA checklist ID')
    for row in result['items']:
        requirements = expected[row['id']]['elements']
        if [e['requirement'] for e in row['elements']] != requirements:
            raise ValueError(f"Incorrect essential elements for PRISMA item {row['id']}")
        for i, element in enumerate(row['elements'], 1):
            target = f"#answer-{row['id']}-{i}" if element['passages'] else None
            if element['location'] != target or (element['status'] in ('complete', 'inapplicable') and not target):
                raise ValueError(f"Missing or incorrect answer location for PRISMA item {row['id']}")


def html(result: Report) -> str:
    lines = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
             '<title>PRISMA checklist</title><style>body{font:1rem/1.5 system-ui;max-width:68rem;margin:2rem auto;padding:0 1rem}'
             'td,th{padding:.4rem .7rem;text-align:left;vertical-align:top}article{margin:1rem 0}blockquote{white-space:pre-wrap}</style><main>',
             '<h1>PRISMA checklist</h1>', '<p>' + escape(result['question']) + '</p>',
             '<h2>Abstract</h2>', '<p>Counts recorded by the run</p>', '<p>' + ' · '.join(escape(count_text(c)) for c in result['counts']) + '</p>']
    included = next(item for item in result['items'] if item['id'] == 'abstract-7')
    for label, element in zip(('Included studies', 'Participants'), included['elements'][:2]):
        for passage in element['passages']:
            if passage['supplier'] == 'author':
                lines.append('<p>' + label + ' · author statement: <a href="' + '#decision-' + escape(passage['evidence'][0])
                             + '">' + escape(passage['text']) + '</a></p>')
    lines.append('<table><thead><tr><th>Item</th><th>Reporting status</th><th>Recorded answers</th></tr></thead><tbody>')
    sections = []
    last_heading = None
    for item in result['items']:
        links = []
        if item['heading'] != last_heading:
            last_heading = item['heading']
            lines.append('<tr><th colspan="3">' + escape(item['heading']) + '</th></tr>')
        title = escape(item['id'] + ' · ' + item['title'])
        sections.append(f'<section id="item-{item["id"]}"><h2>{title}</h2>')
        for i, element in enumerate(item['elements'], 1):
            anchor = f'answer-{item["id"]}-{i}'
            if element['location']:
                links.append(f'<a href="#{anchor}">Element {i}</a>')
            sections.append(f'<article id="{anchor}"><strong>{escape(element["status"].capitalize())}</strong> · '
                            + escape(element['requirement']))
            for p in element['passages']:
                attribution = ''
                if p['supplier'] == 'author':
                    entry = p['evidence'][0]
                    attribution = (f'<a id="decision-{escape(entry)}" href="../review-decisions?last_entry='
                                   + quote(entry, safe='') + '">Recorded decision</a>')
                sections.append(attribution + '<blockquote>' + escape(p['text']) + '</blockquote><p>' + escape(p['supplier'].capitalize())
                                + ' · ' + escape(' · '.join(p['evidence'])) + '</p>')
            if element['basis'] not in ('saved_run', 'reported', 'abstract_checklist'):
                sections.append('<p>' + escape(element['basis'].replace('_', ' ').capitalize()) + '</p>')
            sections.append('</article>')
        if item['see_also']:
            sections.append('<p>' + ' · '.join(f'<a href="#item-{n}">{n}</a>' for n in item['see_also']) + '</p>')
        sections.append('</section>')
        lines.append(f'<tr><td><a href="#item-{item["id"]}">{title}</a></td><td>{item["status"].capitalize()}</td>'
                     + '<td>' + ' · '.join(links) + '</td></tr>')
    return '\n'.join([*lines, '</tbody></table>', *sections, '<footer>', escape(result['citation']),
                      ' · '.join('<a href="' + escape(url) + '">' + label + '</a>'
                                 for url, label in zip(result['sources'], ['Main and expanded checklists', 'Abstract checklist'])),
                      '<a href="' + escape(result['license']) + '">CC BY 4.0</a></footer></main></html>'])
