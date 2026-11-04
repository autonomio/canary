"""Provider-attributed paper metadata; observations, never evidence-quality scores."""
from __future__ import annotations

from copy import deepcopy
from typing import Literal, TypedDict, cast


class PaperAttributes(TypedDict):
    doi: str | None
    ids: dict | None
    display_name: str | None
    publication_year: int | None
    publication_date: str | None
    cited_by_count: int | None
    counts_by_year: list[dict] | None
    fwci: float | None
    citation_normalized_percentile: dict | None
    cited_by_percentile_year: dict | None
    authorships: list[dict] | None
    primary_location: dict | None
    best_oa_location: dict | None
    locations: list[dict] | None
    open_access: dict | None
    type: str | None
    language: str | None
    primary_topic: dict | None
    topics: list[dict] | None
    keywords: list[dict] | None
    study_designs: list[dict] | None
    is_retracted: bool | None
    funders: list[dict] | None
    awards: list[dict] | None
    referenced_works: list[str] | None
    referenced_works_count: int | None
    updated_date: str | None


class PaperMetadata(TypedDict):
    version: Literal[1]
    provider: Literal['openalex']
    provider_id: str
    retrieved_at: str
    attributes: PaperAttributes


# Extend the existing discovery response, without additional metadata requests.
OPENALEX_SELECT = ','.join(dict.fromkeys((
    'id', 'title', 'abstract_inverted_index', *PaperAttributes.__annotations__)))


def snapshot(work: dict, retrieved_at: str) -> PaperMetadata:
    """Keep provider values, including null, zero, false and empty collections.

    The timestamp records the metadata response, not full-paper acquisition.
    Missing fields are unknown; a missing retraction flag is not a clean bill.
    Author lists retain the provider response's coverage, not inferred completeness.
    """
    return {'version': 1, 'provider': 'openalex', 'provider_id': work['id'],
            'retrieved_at': retrieved_at,
            'attributes': cast(PaperAttributes, {key: deepcopy(work.get(key))
                                               for key in PaperAttributes.__annotations__})}
