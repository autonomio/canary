"""One page of OpenAlex discovery through Canary's recorded provider allowance."""
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TypedDict

from .coverage_protocol import OpenAlexBackoff


class KeywordHit(TypedDict):
    title: str
    authors: list[str]
    year: int | None
    venue: str
    url: str
    abstract: str


class KeywordSearch(TypedDict):
    query: str
    status: Literal['available', 'paused', 'unavailable']
    matched: int | None
    results: list[KeywordHit]
    backoff: OpenAlexBackoff | None
    provider_status: int | None


def search(root: Path, request: dict, *, client=None) -> KeywordSearch:
    from . import coverage_runtime as runtime
    from .retrieval import _openalex_works

    query = request.get('query')
    limit = request.get('limit', 15)
    if set(request) - {'query', 'limit'} or not isinstance(query, str) or not query.strip() or len(query) > 2_000:
        raise ValueError('Enter a search query of 1–2,000 characters.')
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('Result limit must be a whole number from 1 to 200.')
    query = query.strip()
    with TemporaryDirectory(prefix='canary-keyword-') as directory:
        workspace = Path(directory)
        plan = workspace / 'keyword-plan.json'
        runtime.write(plan, {'question': query, 'concepts': [], 'outcomes': [], 'conditions': [],
                             'coverage_aspects': [], 'eligibility': {}, 'queries': [{'id': 'Q1', 'search': query}]})
        progress = runtime.discover(workspace, plan, root=root, pages=1, client=client)
        state = progress['queries']['Q1']
        backoff = progress.get('backoff')
        body = runtime.read(workspace / 'search-responses' / 'Q1-0001.json')
        papers = _openalex_works(body['results'][:limit], '') if body else []
        return {'query': query, 'status': 'available' if body else 'paused' if backoff and backoff['active'] else 'unavailable',
                'matched': state.get('matched'), 'backoff': backoff,
                'provider_status': 200 if body else state.get('failure_status'),
                'results': [{'title': paper.title, 'authors': list(paper.authors), 'year': paper.year,
                             'venue': paper.venue, 'url': paper.url, 'abstract': paper.abstract} for paper in papers]}
