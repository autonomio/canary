"""Citation leads within the OpenAlex records returned for a question."""
from collections import Counter
from typing import Iterable, TypedDict

from .candidate_index import WORK_ID
from .coverage_sources import normalized_doi


class CitationLead(TypedDict):
    work_id: str
    title: str
    cites_anchors: list[str]
    shared_query_ids: list[str]
    incoming_links: int


class CitationLeads(TypedDict):
    anchors: list[str]
    openalex_works: int
    works_with_references: int
    leads: list[CitationLead]


def rank(records: Iterable[dict], anchors: list[str], screened: set[str]) -> CitationLeads:
    works = {}
    for record in records:
        work_id = str(record.get('id', '')).rsplit('/', 1)[-1]
        if not WORK_ID.fullmatch(work_id):
            continue
        raw_references = record.get('referenced_works')
        references = {url.rsplit('/', 1)[-1] for url in raw_references if isinstance(url, str)} \
            if isinstance(raw_references, list) else set()
        works[work_id] = (record, references)
    if not set(anchors) <= works.keys():
        raise ValueError('Citation anchors must be in this run’s OpenAlex results.')

    incoming = Counter(reference for _, references in works.values() for reference in references)
    anchor_queries = {a: set(works[a][0].get('query_ids') or []) for a in anchors}
    used_dois = {normalized_doi(works[a][0].get('doi')) for a in anchors} - {''}
    ranked = []
    for work_id, (work, references) in works.items():
        if work_id in anchor_queries or work_id in screened or work.get('is_retracted') is True:
            continue
        queries = set(work.get('query_ids') or [])
        shared = {a: queries & anchor_queries[a] for a in anchors if a in references}
        cites = [a for a, matches in shared.items() if matches]
        if not cites:
            continue
        query_ids = sorted(set().union(*(shared[a] for a in cites)))
        lead = CitationLead(work_id=work_id, title=work.get('display_name') or work.get('title') or work_id,
                            cites_anchors=cites, shared_query_ids=query_ids, incoming_links=incoming[work_id])
        ranked.append((lead, normalized_doi(work.get('doi'))))
    ranked.sort(key=lambda row: (-len(row[0]['cites_anchors']), -len(row[0]['shared_query_ids']),
                                 -row[0]['incoming_links'], row[0]['work_id']))
    leads = []
    for lead, doi in ranked:
        if doi and doi in used_dois:
            continue
        if doi:
            used_dois.add(doi)
        leads.append(lead)
        if len(leads) == 3:
            break
    return CitationLeads(anchors=anchors, openalex_works=len(works),
                         works_with_references=sum(bool(refs) for _, refs in works.values()), leads=leads)
