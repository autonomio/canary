"""Typed source-first coverage research contracts, independent of saved experiments."""
from __future__ import annotations

from typing import Literal, TypedDict

from .claim_freshness import schema as freshness_schema

RESEARCH_MODEL = "gpt-6.1-sol"
RESEARCH_EFFORT = "xhigh"
AUDIT_MODEL = "gpt-6-astra"
AUDIT_EFFORT = "ultra"


class OpenAlexBackoff(TypedDict):
    provider: Literal['openalex']
    scope: Literal['anonymous', 'configured_key', 'provider', 'legacy_unknown']
    scope_source: Literal['recorded_request', 'operator_attribution', 'legacy_unknown']
    reason: Literal['rate_limited', 'service_unavailable', 'legacy_scope_unknown']
    retry_not_before: str
    retry_not_before_epoch: float
    retry_after: str | None
    retry_after_source: str
    active: bool


class DiscoveryReadiness(TypedDict):
    """Read-only recorded-pause observation for the allowance now selected.

    `available` means no recorded applicable pause blocks that allowance; it
    is not a provider guarantee. No discovery or probe request is made.
    """
    provider: Literal['openalex']
    status: Literal['available', 'paused', 'unknown']
    openalex_allowance: Literal['anonymous', 'configured_key'] | None
    backoff: OpenAlexBackoff | None
    unknown_reason: str | None
    observed_at: str
    basis: Literal['recorded_backoff_only']
    probe_request_made: bool


class OpenAlexRecheck(TypedDict):
    """Outcome of one operator-requested recheck of a recorded discovery hold.

    A successful fresh response releases only the actual allowance's own
    scoped hold. Provider-wide and legacy holds, other allowances, and all
    usage records are never touched; every request is audit-recorded.
    """
    provider: Literal['openalex']
    status: Literal['released', 'still_paused', 'no_hold', 'not_releasable_scope',
                    'cooldown', 'unavailable']
    openalex_allowance: Literal['anonymous', 'configured_key'] | None
    observed_at: str
    probe_made: bool
    http_status: int | None
    released_backoff: OpenAlexBackoff | None
    current_backoff: OpenAlexBackoff | None
    next_recheck_not_before: str | None
    next_recheck_not_before_epoch: float | None


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def arr(items):
    return {"type": "array", "items": items}


def enum(*values):
    return {"type": "string", "enum": list(values)}


S = {"type": "string"}
SS = arr(S)


def question_plan_schema():
    """Search language is typed separately from full-paper verification criteria."""
    return obj({
        "question": S,
        "concepts": arr(obj({"name": S, "synonyms": SS, "search_role": S})),
        "outcomes": SS,
        "comparators": SS,
        "conditions": SS,
        "coverage_aspects": arr(obj({"id": S, "description": S, "priority_reason": S})),
        "eligibility": obj({"core": S, "supporting": S, "exclusion": S,
                            "full_paper_checks": SS}),
        "queries": arr(obj({"id": S, "search": S, "coverage_aspect_ids": SS,
                            "purpose": S, "collapse_reason": S})),
        "scope_note": S,
    })


def investigation_schema():
    field = obj({"text": S, "passage_ids": SS})
    uncertainty = obj({"class": enum("low", "moderate", "high"),
                       "score": {"type": "integer", "enum": [1, 2, 3]}, "rationale": S})
    return obj({
        "executive_summary": arr(obj({"text": S, "claim_ids": SS})),
        "claims": arr(obj({"id": S, "text": S,
            "kind": enum("reported_finding", "author_interpretation", "derived_comparison", "secondary_report"),
            "uncertainty": uncertainty, "freshness": freshness_schema(), "evidence_ids": SS, "study_result_ids": SS})),
        "evidence": arr(obj({"id": S, "passage_ids": SS, "interpretation": S,
                             "relation": enum("supports", "qualifies", "contradicts")})),
        "study_results": arr(obj({"id": S, "reference_ids": SS,
            "result_type": enum("measured", "fitted", "simulated", "association", "qualitative", "secondary_report"),
            "fields": obj({name: {"anyOf": [field, {"type": "null"}]} for name in (
                "system", "intervention", "comparator", "conditions", "endpoint", "value", "units",
                "sample_size", "reported_uncertainty")}), "version_or_cohort_note": S})),
        "contradictions": arr(obj({"id": S,
            "kind": enum("within_source", "between_sources", "between_measurement_and_model"),
            "description": S, "claim_ids": SS, "evidence_ids": SS, "comparability": S,
            "resolution": enum("unresolved", "explained_by_scope", "source_discrepancy"), "why_it_matters": S})),
        "gaps": arr(obj({"id": S, "kind": enum("unanswered_scope", "missing_data", "unmatched_conditions",
            "access_limit", "measurement_limit", "conflicting_evidence"), "description": S, "basis": S,
            "claim_ids": SS, "evidence_ids": SS, "needed_to_resolve": S})),
        "residue": arr(obj({"id": S, "observation": S, "why_it_matters": S, "basis": S,
            "claim_ids": SS, "evidence_ids": SS, "scope_limit": S})),
        "next_questions": arr(obj({"id": S, "question": S, "justification": S,
                                   "gap_ids": SS, "residue_ids": SS})),
        "caveats": SS, "scope_note": S,
    })


def audit_schema():
    return obj({
        "claim_checks": arr(obj({"claim_id": S, "assessment": enum("supported", "correction_needed", "unresolved", "not_checked"),
            "reason": S, "passage_ids": SS, "reference_ids": SS, "context_checked": SS})),
        "findings": arr(obj({"id": S, "severity": enum("material", "minor", "uncertain"),
            "description": S, "claim_ids": SS, "reference_ids": SS, "passage_ids": SS})),
        "scores": obj({area: obj({"score": {"type": "integer", "minimum": 0, "maximum": 5}, "reason": S})
            for area in ("selection_defensibility", "question_relevance", "scientific_fidelity", "evidence_coverage", "information_value")}),
        "source_reviewed_ids": SS, "selection_assessment": S, "strengths": SS,
        "limitations": SS, "audit_complete": {"type": "boolean"}, "scope_note": S,
    })


SCIENTIFIC_RULES = """Use one stateful session; no subagents, other models, nested CLI sessions or delegation.
Work backwards from fixed source passages to attributed claims. Verify methods/results and surrounding source context.
Keep experiments, cohorts, versions, denominators, comparators and units distinct. Use code for arithmetic and navigation.
Distinguish measured, fitted, simulated, associated and qualitative results. Nonsignificance is not equivalence.
Unreported conditions remain unknown. Preserve adverse/null evidence and source contradictions without manufacturing resolution.
Claim uncertainty low=1/moderate=2/high=3 is an ordinal model judgment, not a probability or reported source uncertainty.
Every claim requires freshness: source as_of (ISO date or null), policy scheduled/event_driven/historical/unknown, review_after (ISO date for scheduled only), triggers and rationale. A review horizon requests reassessment, not automatic truth expiry. Historical observations need no scheduled expiry; never invent a source date or review deadline. Record unknown where no defensible horizon is available.
Never treat abstracts, metadata, a paywall preview or an opened but unexamined document as a substantively reviewed full paper.
Report publication counts separately from independent study families and core comparisons separately from supporting context.
No universal absence/recall assertion from this selected corpus. Keep output question-driven and compact, usually several dozen claims maximum.
Do not manually copy quotations into the final evidence; cite canonical passage IDs so code binds reproducible verbatim.
Preserve raw findings; the subsequent independent audit is separate, with checked and unchecked scope explicit.
"""


LOCAL_TEXT_SCOPE = """
This run uses a bounded local-text corpus, never public discovery or downloads.
Read local-corpus.json for the hashed inventory, excluded files, metadata coverage and scope.
Queries use SQLite FTS5 lexical MATCH syntax: words joined with AND/OR/NOT; quote phrases and hyphenated words.
No semantic search is performed. Broaden with synonyms where needed. Candidate discovery_excerpt is a text preview,
not an abstract or proof of full-publication completeness. supporting_abstract_text must use actual metadata abstracts or null.
Select the returned L-prefixed IDs. Acquire imports only unchanged bytes from the indexed files; no network fallback.
Saved originals are the supplied text files. Layout, tables, figures, identity and versions may be missing or damaged.
Verify source identity and substantive methods/results before counting a paper; mark incomplete sources as preview/uncertain.
Every coverage, scarcity and absence statement is restricted to this indexed corpus and the actual inspected scope.
New runs rescan the configured directory; repeating settings does not freeze external input files.
"""


def research_prompt(question, command, minimum_papers=50, *, local_text=False):
    search = (LOCAL_TEXT_SCOPE if local_text else
        "Queries must use OpenAlex full-text-inclusive search=; abstract/title-only filters are prohibited. "
        "Collapse genuinely shared root queries while preserving question relevance.\n"
        "Understand: discovery searches indexed full text plus abstracts/titles; relevance triage then reads candidate abstracts, not full papers.")
    return f"""Read the repository AGENTS.md first, then this workspace AGENTS.md. This is real research.
Question: {question}
{SCIENTIFIC_RULES}
1. Write question-plan-draft.json conforming to question-plan-schema.json. Use 'concepts', never 'entities'.
Translate the question into concepts/synonyms, outcomes, comparators, conditions, coverage aspects and explicit inclusion/exclusion criteria.
{search}
Discovery alone commits the validated question-plan.json; never overwrite that canonical file directly.
2. Run {command} discover --plan question-plan-draft.json. Read search-progress.json and candidates.jsonl.
Choose batches by coverage-guided marginal information gain: direct comparisons, contrasting/adverse/null findings, condition/outcome differences, methods, then needed context.
Record selection.json as {{method:'coverage', approach, assessed_candidate_ids, decisions:[{{work_id,decision:'include|exclude|defer',evidence_role:'core|supporting|unknown',reason,criterion_matches,unknowns,supporting_abstract_text,priority_rank,coverage_contribution}}], selected_for_review_ids, stopping_reason, selection_limitations}}.
Exact abstract support or null; missing abstracts are uncertainty, not automatic exclusion. All omitted candidates remain unassessed.
3. Run {command} acquire --ids SOURCE_ID SOURCE_ID for only your selected batch. Launch acquisition in the background if useful;
read committed sources using {command} catalog and {command} source WORK_ID --match REGEX --context 2 --limit 8.
Do useful screening, reading or synthesis while downloads proceed; never wait idly for an unavailable paper.
Originals are papers/, full extracted texts texts/, immutable source spans passages/, per-paper receipts download-receipts/.
Every retrieved source still requires identity, completeness and question relevance verification; source status is not scientific certification.
4. Substantively investigate at least {minimum_papers} question-relevant accessible full publications, unless actual scarcity is documented.
Count unique publications, not versions. Do not pad with generic context. Persist reviewed-papers.json after each completed source review:
[{{work_id,eligibility:'core|supporting|preview|irrelevant|uncertain',reason,investigated_sections,passage_ids_read,incorporated_in_output,related_study_ids,substantive_review_note}}].
Notes must reflect actual question-linked reading of methods/results or equivalent context, not templated status notes.
Update progress.jsonl with UTC milestones and admitted core/supporting counts. Keep coverage-map.json by plan aspect with direct/supporting source IDs and unresolved needs.
If fewer than {minimum_papers} remain, edit question-plan-draft.json with targeted supplemental queries and repeat discover/acquire; preserve existing query IDs/searches and assign a new ID whenever changing a search string; record actual assessed exhaustion and access limits in scarcity.json.
A timeout or inaccessible copy alone does not prove topic scarcity. The target is not {minimum_papers} independent trials: role/family accounting is essential.
5. Save investigation.json conforming to investigation-schema.json, and return that same JSON as the final response.
Claims -> evidence -> canonical passages -> references; executive prose links claims. Study-result fields each cite actual supporting passages or are null.
Include typed contradictions, gaps, residue, justified next questions, caveats and honest reviewed scope.
Residue preserves relevant material that is neither a claim nor a gap: what must not be lost. Bind its basis, claim/evidence links and scope; do not require a resolution, alternative explanation or follow-up test. Gaps describe what remains unanswered and what would resolve it.
No earlier experiment assets, paper lists, answers or audits. No outputs from other sessions. Complete the actual work in this session.
"""


def audit_prompt(question, command, budget_seconds=600):
    return f"""Read repository AGENTS.md first, then workspace AGENTS.md. Independently audit this real research question:
{question}
{SCIENTIFIC_RULES}
Inspect audit-input.json, question-plan.json, selection.json, reviewed-papers.json and the current fresh source corpus.
Never inspect researcher reasoning transcripts, CLI events/stderr/prompts, previous research workspaces or previous audits.
Use {command} source WORK_ID --match REGEX --context 3 --limit 10 for source rereading; originals are available for table/figure checks.
Audit the actual claim/result scope, exact source meaning, arithmetic, experiment attribution, selection/coverage and paper-role/family counts.
Do not reward paper quantity or confidence; identify irrelevant padding, preview inclusion, important adverse alternatives and unsupported claims.
Budget {budget_seconds} seconds. Track your own elapsed time. Persist audit.json as you go; if unfinished, explicitly label every remaining claim not_checked.
Scores 0-5 are judgments within inspected scope, not calibrated probabilities or population recall. 3 means useful but consequential limitations; 5 requires strong inspected support.
Never rewrite the research answer or invoke another agent/model. Return audit-schema.json JSON; audit_complete=true only if all declared audit scope is actually checked.
"""
