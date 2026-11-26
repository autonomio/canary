"""PRISMA 2020 reporting checklist for saved reviews."""
from functools import lru_cache
from html import escape
from typing import Literal, TypedDict
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
    requirement: str
    status: Status
    location: str | None
    passages: list[Passage]


class Item(TypedDict):
    id: str
    title: str
    status: Status
    elements: list[Element]


class Report(TypedDict):
    version: str
    citation: str
    license: str
    sources: list[str]
    question: str
    edition: str
    items: list[Item]


def _run_answers(view: dict) -> dict:
    answers = {}
    def add(key, text, path, status='partial'):
        if isinstance(text, str) and text.strip():
            answers[key] = (status, [dict(text=text.strip(), evidence=[path], supplier='run')])

    graph = view.get('answer', {})
    question = graph.get('question')
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
    return answers


def _status(statuses: list[Status]) -> Status:
    if all(s == 'inapplicable' for s in statuses):
        return 'inapplicable'
    if all(s in ('complete', 'inapplicable') for s in statuses):
        return 'complete'
    return 'partial' if any(s != 'missing' for s in statuses) else 'missing'


def report(view: dict, edition: dict) -> Report:
    data = catalogue()
    answers = _run_answers(view)
    entries = edition['entries']
    replaced = {entry['replaces'] for entry in entries if entry.get('replaces')}
    author_answers: dict[str, list[Passage]] = {}
    for entry in entries:
        d = entry['decision']
        subject = entry.get('subject_id') or d.get('subject_id', '')
        if (entry['id'] in replaced or not subject.startswith('prisma:') or
                d['status'] not in ('accepted', 'modified') or (d.get('decided_by') or {}).get('kind') != 'person' or
                d['judgement'] != 'reported'):
            continue
        author_answers.setdefault(subject.removeprefix('prisma:'), []).append(dict(
            text=d['reason'], evidence=[entry['id'], *d['evidence']], supplier='author'))
    items: list[Item] = []
    for definition in data['items']:
        number = definition['id']
        elements: list[Element] = []
        for i, requirement in enumerate(definition['elements'], 1):
            key = f'{number}:{i}'
            status, passages = answers.get(key, ('missing', []))
            if key in author_answers:
                passages = [*passages, *author_answers[key]]
                status = 'complete' if len(author_answers[key]) == 1 else 'partial'
            location = f'#answer-{number}-{i}' if passages else None
            elements.append(dict(requirement=requirement, status=status, location=location, passages=passages))
        items.append(dict(id=number, title=definition['title'], status=_status([e['status'] for e in elements]),
                          elements=elements))
    return dict(version=data['version'], citation=data['citation'], license=data['license'], sources=data['sources'],
                question=view.get('answer', {}).get('question') or 'Research review', edition=edition['last_entry'], items=items)


def html(result: Report) -> str:
    lines = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
             '<title>PRISMA checklist</title><style>body{font:1rem/1.5 system-ui;max-width:68rem;margin:2rem auto;padding:0 1rem}'
             'td,th{padding:.4rem .7rem;text-align:left;vertical-align:top}article{margin:1rem 0}blockquote{white-space:pre-wrap}</style><main>',
             '<h1>PRISMA checklist</h1>', '<p>' + escape(result['question']) + '</p>',
             '<table><thead><tr><th>Item</th><th>Reporting status</th><th>Recorded answers</th></tr></thead><tbody>']
    sections = []
    for item in result['items']:
        links = []
        title = escape(item['id'] + ' · ' + item['title'])
        sections.append(f'<section id="item-{item["id"]}"><h2>{title}</h2>')
        for i, element in enumerate(item['elements'], 1):
            anchor = f'answer-{item["id"]}-{i}'
            if element['location']:
                links.append(f'<a href="#{anchor}">Element {i}</a>')
            sections.append(f'<article id="{anchor}"><strong>{escape(element["status"].capitalize())}</strong> · '
                            + escape(element['requirement']))
            for p in element['passages']:
                sections.append('<blockquote>' + escape(p['text']) + '</blockquote><p>' + escape(p['supplier'].capitalize())
                                + ' · ' + escape(' · '.join(p['evidence'])) + '</p>')
            sections.append('</article>')
        sections.append('</section>')
        lines.append(f'<tr><td><a href="#item-{item["id"]}">{title}</a></td><td>{item["status"].capitalize()}</td>'
                     + '<td>' + ' · '.join(links) + '</td></tr>')
    return '\n'.join([*lines, '</tbody></table>', *sections, '<footer>', escape(result['citation']),
                      '<a href="' + escape(result['license']) + '">CC BY 4.0</a></footer></main></html>'])
