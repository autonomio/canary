"""Publish the coverage-guided paper investigation to Canary's source graph.

This adapter binds committed verbatim and recorded audits. It never rewrites a
scientific claim, estimates a benefit/effort ratio, or treats an audit as truth.
"""
from __future__ import annotations

import json
import os
import hashlib
import shutil
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

EXECUTION_MODE = "coverage-guided full-paper research"


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".export.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _unique(values):
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _copy(source: Path, target: Path):
    """Keep saved files inside the bundle for the existing source-context API."""
    if source.resolve() == target.resolve():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".export.tmp")
    temporary.unlink(missing_ok=True)
    try:
        os.link(source, temporary)
    except OSError:
        shutil.copyfile(source, temporary)
    temporary.replace(target)


def _normalize_receipts(receipts):
    result = []
    for receipt in _rows(receipts):
        usage = dict(receipt.get("usage", {}))
        if "cached_input_tokens" in usage and "input_tokens_details" not in usage:
            usage["input_tokens_details"] = {"cached_tokens": usage.get("cached_input_tokens")}
        if "reasoning_output_tokens" in usage and "output_tokens_details" not in usage:
            usage["output_tokens_details"] = {"reasoning_tokens": usage.get("reasoning_output_tokens")}
        result.append(dict(receipt, usage=usage))
    return result


def _profile_roles(profile: dict) -> dict:
    direct = profile.get("roles")
    if isinstance(direct, dict):
        return direct
    result = {}
    for role in ("research", "audit"):
        record = profile.get(role)
        if not isinstance(record, dict):
            continue
        usage = record.get("usage", {})
        usage = usage if isinstance(usage, dict) else {}
        result[role] = dict(record, tokens=usage.get("totals", record.get("tokens", {})),
                            receipts=usage.get("receipts", record.get("receipts", [])))
    return result


def _profile_receipts(profile: dict) -> list[dict]:
    direct = _rows(profile.get("receipts"))
    if direct:
        return _normalize_receipts(direct)
    result = []
    for role, record in _profile_roles(profile).items():
        if not isinstance(record, dict):
            continue
        receipts = _rows(record.get("receipts"))
        if receipts:
            result.extend(dict(receipt, role=role) for receipt in receipts)
            continue
        tokens = record.get("tokens", {})
        if not isinstance(tokens, dict):
            tokens = {}
        if not tokens and not record.get("usage"):
            continue
        inp = tokens.get("input_tokens")
        out = tokens.get("output_tokens")
        cached = tokens.get("cached_input_tokens", tokens.get("cached_tokens"))
        result.append({"provider": "codex_harness", "role": role,
                       "aggregation": "role total; per-response receipts in profile",
                       "usage": {"input_tokens": inp, "output_tokens": out,
                                 "input_tokens_details": {"cached_tokens": cached},
                                 "output_tokens_details": {"reasoning_tokens": tokens.get("reasoning_output_tokens")},
                                 "total_tokens": inp + out if type(inp) is int and type(out) is int else None}})
    return _normalize_receipts(result)


def _audit_record(audit: dict) -> dict:
    # One production audit is direct; the comparison format is also accepted.
    cases = _rows(audit.get("case_evaluations"))
    return cases[0] if len(cases) == 1 else audit


def _save_markdown(path: Path, graph: dict):
    """Deterministic downloadable answer; researcher wording stays unchanged."""
    lines = ["# " + str(graph.get("question", "?")), "", str(graph.get("scope_note", "")), ""]
    for summary in _rows(graph.get("executive_summary")):
        lines.extend([str(summary.get("text", "")), "Claims: " + ", ".join(summary.get("claim_ids", [])), ""])
    checks = {r.get("claim_id"): r for r in _rows(graph.get("source_checks"))}
    evidence = {r.get("id"): r for r in _rows(graph.get("evidence"))}
    passages = {r.get("id"): r for r in _rows(graph.get("passages"))}
    for claim in _rows(graph.get("claims")):
        lines.extend(["## " + str(claim["id"]), "", str(claim.get("text", "")), "",
                      f'Uncertainty: {claim.get("uncertainty_class", "unknown")} ({claim.get("uncertainty_score", "unknown")}/3; ordinal).',
                      str(claim.get("uncertainty_rationale", "")), "Audit: " + str(checks.get(claim["id"], {}).get("assessment", "not_checked")), ""])
        for eid in claim.get("evidence_ids", []):
            item = evidence.get(eid, {})
            span = passages.get(item.get("paragraph_id"), {})
            lines.extend([f'Evidence {eid} · {item.get("relation", "supports")} · {span.get("source_id", "?")} / {item.get("paragraph_id", "?")}',
                          str(item.get("interpretation", "")), "", *["> " + line for line in str(item.get("quote", "")).splitlines()], ""])
    for key, label in (("contradictions", "Contradictions"), ("gaps", "Gaps"), ("residue", "Residue"), ("next_questions", "Next questions")):
        rows = _rows(graph.get(key))
        if rows:
            lines.extend(["## " + label, ""])
            for row in rows:
                lines.extend([str(row.get("description", row.get("observation", row.get("question", "")))),
                              str(row.get("why_it_matters", row.get("justification", ""))), ""])
    if graph.get("caveats"):
        lines.extend(["## Caveats", "", *[str(x) for x in graph["caveats"]], ""])
    lines.extend(["## References", ""])
    for ref in _rows(graph.get("references")):
        paper = ref.get("paper", {})
        lines.append(f'{ref.get("id")} · {paper.get("title", "Unknown source")} · {paper.get("doi") or paper.get("url") or "See saved source graph"}')
    temporary = path.with_name(path.name + ".export.tmp")
    temporary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


def export_bundle(bundle: str | Path, *, source_dir: str | Path | None = None,
                  research_dir: str | Path | None = None) -> dict:
    """Export committed stages into answer/run/access artifacts, including live runs.

    Source, research and audit JSON remain the canonical scientific records.
    Missing fields remain unknown. ``run.json`` owns lifecycle status/timestamps;
    the runtime must set them before calling this function.
    """
    bundle = Path(bundle).resolve()
    source = Path(source_dir).resolve() if source_dir else next(
        (p for p in (bundle / "common", bundle / "research") if (p / "passages").is_dir()), bundle)
    research = Path(research_dir).resolve() if research_dir else (
        bundle / "research" if (bundle / "research").is_dir() else bundle)
    payload = _load(research / "payload.json", {})
    payload = payload if isinstance(payload, dict) else {}
    investigation = _load(research / "investigation.json", payload.get("research", payload))
    investigation = investigation if isinstance(investigation, dict) else {}
    selection = _load(research / "selection.json", payload.get("selection", {}))
    selection = selection if isinstance(selection, dict) else {}
    reviewed = _rows(_load(research / "reviewed-papers.json", payload.get("reviewed_papers", [])))
    plan = _load(bundle / "question-plan.json", _load(source / "question-plan.json", payload.get("question_plan", {})))
    plan = plan if isinstance(plan, dict) else {}
    run = _load(bundle / "run.json", {})
    run = run if isinstance(run, dict) else {}
    context = _load(bundle / "run-context.json", {})
    question = plan.get("question") or run.get("question") or context.get("question") or "?"
    refs = {r["id"]: r for r in _rows(_load(source / "references.json", payload.get("references", []))) if r.get("id")}
    receipts = []
    receipt_dir = source / "download-receipts" if (source / "download-receipts").is_dir() else bundle / "download-receipts"
    for path in sorted(receipt_dir.glob("*.json")):
        receipt = _load(path, {})
        if not isinstance(receipt, dict):
            continue
        disposition = receipt.get("disposition", receipt)
        receipts.append(disposition)
        ref = receipt.get("reference")
        if isinstance(ref, dict) and ref.get("id"):
            refs[ref["id"]] = ref
    candidates = {}
    try:
        for line in (source / "candidates.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if isinstance(row, dict) and row.get("id"):
                candidates[row["id"]] = row
    except (OSError, ValueError):
        pass
    spans = {}
    for path in sorted((source / "passages").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if isinstance(row, dict) and row.get("id"):
                spans[row["id"]] = row
    # Older acquisition receipts may predate a newly observed preview marker.
    # Derive its label from committed source text without rewriting that receipt.
    for ref in refs.values():
        try:
            text = (source / ref["text_file"]).read_text(encoding="utf-8")
        except (KeyError, OSError):
            continue
        if re.search(r"article[- ]preview|section snippets|access through your organization|purchase this article|preview\s+of\s+subscription\s+content", text, re.I):
            ref.update(preview_marker_detected=True, full_text_status="preview_suspected")
    preview_count = sum(bool(r.get("preview_marker_detected") or r.get("full_text_status") == "preview_suspected") for r in refs.values())
    selected = set(selection.get("selected_for_review_ids", []))
    admitted = {r["work_id"]: r for r in reviewed
                if r.get("work_id") in selected and r.get("eligibility") in ("core", "supporting")
                and r.get("passage_ids_read") and str(r.get("substantive_review_note", "")).strip() and r.get("investigated_sections")
                and r.get("work_id") in refs and not refs[r["work_id"]].get("preview_marker_detected")
                and refs[r["work_id"]].get("full_text_status") != "preview_suspected"
                and all(pid in spans and spans[pid].get("reference_id", spans[pid].get("source_id")) == r["work_id"]
                        for pid in r.get("passage_ids_read", []))}
    publications, counted, seen_bodies = {}, {}, set()
    for sid, row in admitted.items():
        ref = refs[sid]
        doi = str(ref.get("doi") or "").casefold().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
        body = ref.get("body_sha256")
        identity = doi or body or sid
        if identity in publications or body and body in seen_bodies:
            continue
        publications[identity] = [row]
        counted[sid] = row
        if body:
            seen_bodies.add(body)
    admitted = counted
    roles = Counter(row["eligibility"] for row in admitted.values())
    graph = {"question": question, "model": run.get("model", "gpt-6.1-sol"),
             "executive_summary": investigation.get("executive_summary", []), "claims": [],
             "evidence": [], "passages": [], "references": [], "study_results": [],
             "caveats": investigation.get("caveats", []), "next_questions": investigation.get("next_questions", []),
             "unanswered": [dict(g, text=g.get("description", "")) for g in _rows(investigation.get("gaps"))],
             "gaps": investigation.get("gaps", []), "contradictions": investigation.get("contradictions", []),
             "residue": investigation.get("residue", []), "scope_note": investigation.get("scope_note", ""),
             "source_check_method": "astra_full_paper_audit", "source_check_model": "gpt-6-astra",
             "source_check_reasoning_effort": "ultra", "source_checks": [], "binding_issues": []}
    efficiency = _load(research / "efficiency.json", _load(bundle / "source-efficiency.json", {}))
    efficiency = efficiency if isinstance(efficiency, dict) else {}
    used_passages = {pid for comparison in _rows(efficiency.get("comparisons"))
                     for pid in _unique(comparison.get("passage_ids", []))}
    evidence_map = {}
    for evidence in _rows(investigation.get("evidence")):
        identifiers = _unique(evidence.get("passage_ids", []))
        evidence_map[evidence.get("id")] = []
        for n, pid in enumerate(identifiers, 1):
            span = spans.get(pid)
            if span is None:
                graph["binding_issues"].append({"kind": "missing_passage", "evidence_id": evidence.get("id"), "passage_id": pid})
                continue
            eid = evidence["id"] if len(identifiers) == 1 else f'{evidence["id"]}.{n}'
            graph["evidence"].append({"id": eid, "origin_evidence_id": evidence["id"], "paragraph_id": pid,
                                      "quote": span.get("text", ""), "interpretation": evidence.get("interpretation", ""),
                                      "relation": evidence.get("relation", "supports")})
            evidence_map[evidence["id"]].append(eid)
            used_passages.add(pid)
    for claim in _rows(investigation.get("claims")):
        u = claim.get("uncertainty", {})
        graph["claims"].append(dict(claim, uncertainty_class=u.get("class"), uncertainty_score=u.get("score"),
                                     uncertainty_rationale=u.get("rationale", ""),
                                     evidence_ids=[eid for key in claim.get("evidence_ids", []) for eid in evidence_map.get(key, [])]))
    field_map = {"system": "setup", "intervention": "intervention", "comparator": "comparator",
                 "conditions": "conditions", "endpoint": "outcome", "value": "value", "units": "units",
                 "sample_size": "sample_size", "reported_uncertainty": "uncertainty"}
    type_map = {"measured": "observed_measurement", "fitted": "fitted_estimate", "simulated": "predicted_contrast"}
    for record in _rows(investigation.get("study_results")):
        fields = {}
        for name, f in record.get("fields", {}).items():
            if isinstance(f, dict):
                ids = _unique(f.get("passage_ids", []))
                fields[field_map.get(name, name)] = {"value": f.get("text"), "paragraph_ids": ids}
                used_passages.update(ids)
        sources = _unique(record.get("reference_ids", []))
        graph["study_results"].append(dict(record, source_id=sources[0] if len(sources) == 1 else None,
                                             source_ids=sources, fields=fields,
                                             result_type=type_map.get(record.get("result_type"), record.get("result_type"))))
    audit = _load(bundle / "audit" / "audit.json", _load(bundle / "audit.json", {}))
    audit = audit if isinstance(audit, dict) else {}
    audit_case = _audit_record(audit)
    checks = {c.get("claim_id"): c for c in _rows(audit_case.get("claim_checks"))}
    for claim in graph["claims"]:
        check = checks.get(claim["id"], {})
        assessment = check.get("assessment", "not_checked")
        # "Corrected" can describe a proposed correction, not a published rewrite.
        normalized = {"correction_needed": "correction_needed", "partly_supported": "unresolved"}.get(assessment, assessment)
        ids = _unique(check.get("passage_ids", check.get("paragraph_ids", [])))
        used_passages.update(ids)
        graph["source_checks"].append(dict(check, claim_id=claim["id"], assessment=normalized,
                                             raw_assessment=assessment, paragraph_ids=ids,
                                             reason=check.get("reason", "Audit pending." if not audit else "Not checked in the recorded audit scope."),
                                             correction_applied=False))
    used_sources = set(admitted)
    source_texts = {}
    for pid in sorted(used_passages):
        span = spans.get(pid)
        if not span:
            graph["binding_issues"].append({"kind": "missing_linked_passage", "passage_id": pid})
            continue
        sid = span.get("reference_id", span.get("source_id"))
        used_sources.add(sid)
        ref = refs.get(sid, {})
        if sid not in source_texts:
            try:
                source_texts[sid] = (source / ref["text_file"]).read_text(encoding="utf-8")
            except (KeyError, OSError):
                source_texts[sid] = None
        text = source_texts[sid]
        a, z = span.get("start"), span.get("end")
        anchored = (isinstance(text, str) and type(a) is int and type(z) is int
                    and 0 <= a < z <= len(text) and text[a:z] == span.get("text"))
        if not anchored:
            graph["binding_issues"].append({"kind": "span_mismatch", "passage_id": pid})
        graph["passages"].append(dict(span, source_id=sid, kind="fulltext", anchored=anchored))
    anchored_by_id = {p["id"]: p["anchored"] for p in graph["passages"]}
    for evidence in graph["evidence"]:
        evidence["anchored"] = anchored_by_id.get(evidence["paragraph_id"], False)
    accepted = []
    for sid in sorted(s for s in used_sources if isinstance(s, str)):
        ref = refs.get(sid)
        if not ref:
            graph["binding_issues"].append({"kind": "missing_reference", "reference_id": sid})
            continue
        c = candidates.get(sid, {})
        paper_path = source / ref.get("file", "")
        text_path = source / ref.get("text_file", "")
        local_file = "papers/" + paper_path.name if paper_path.is_file() else None
        local_text = f"papers/{sid}.txt" if text_path.is_file() else None
        if local_file:
            if ref.get("body_sha256") and hashlib.sha256(paper_path.read_bytes()).hexdigest() != ref["body_sha256"]:
                graph["binding_issues"].append({"kind": "body_hash_mismatch", "reference_id": sid})
            _copy(paper_path, bundle / local_file)
        if local_text:
            if ref.get("text_sha256") and hashlib.sha256(text_path.read_bytes()).hexdigest() != ref["text_sha256"]:
                graph["binding_issues"].append({"kind": "text_hash_mismatch", "reference_id": sid})
            _copy(text_path, bundle / local_text)
        paper = {"ref": sid, "title": ref.get("title", c.get("title", sid)), "doi": ref.get("doi", c.get("doi")),
                 "authors": ref.get("authors", []), "year": ref.get("year", c.get("year")),
                 "url": "https://openalex.org/" + sid, "abstract": c.get("abstract", ""),
                 "extra": {"local_file": local_file, "text_sha256": ref.get("text_sha256"), "body_sha256": ref.get("body_sha256")}}
        graph["references"].append({"id": sid, "kind": "fulltext", "paper": paper, "text_url": ref.get("url"),
                                    "text_chars": ref.get("text_chars"), "review": admitted.get(sid),
                                    "role": admitted.get(sid, {}).get("eligibility", "cited_context")})
        if local_file and local_text:
            accepted.append({"paper": paper, "file_path": local_file, "text_path": local_text,
                             "content_kind": ref.get("content_kind"), "text_chars": ref.get("text_chars"),
                             "reviewed": sid in admitted, "cached_credits": 0})
    groups = {}
    for row in admitted.values():
        for group in _unique(row.get("related_study_ids", [])):
            groups.setdefault(group, []).append(row["work_id"])
    covered = len(publications)
    target = run.get("minimum_papers", run.get("target_papers", run.get("target_publications", 50)))
    coverage = {"selection_method": "coverage-guided", "target_papers": target, "reviewed_papers": covered,
                "core_papers": roles.get("core", 0), "supporting_papers": roles.get("supporting", 0),
                "cited_sources": len({p["source_id"] for p in graph["passages"]}),
                "available_papers": len(refs) - preview_count, "retrieved_sources": len(refs),
                "preview_suspected_sources": preview_count, "discovered_candidates": len(candidates),
                "assessed_candidates": len(set(selection.get("assessed_candidate_ids", []))),
                "decision_records": len(_rows(selection.get("decisions"))), "related_report_groups": groups,
                "independent_study_count": None,
                "duplicate_version_groups": [[r["work_id"] for r in group] for group in publications.values() if len(group) > 1],
                "stopping_reason": selection.get("stopping_reason"),
                "counts_scope": "Reviewed papers require selected admission, core/supporting eligibility, read passages and a substantive review note. Related reports may share participants or studies; paper count is not independent study count."}
    graph["coverage"] = coverage
    profile = _load(bundle / "profile.json", {})
    profile = profile if isinstance(profile, dict) else {}
    profile_roles = dict(_profile_roles(profile))
    for role in ("research", "audit"):
        receipt = _load(bundle / (role + "-receipt.json"), _load(research / (role + "-receipt.json"), None))
        if isinstance(receipt, dict) and receipt.get("ended_at"):
            usage = receipt.get("usage", {})
            profile_roles[role] = dict(receipt, active=False, tokens=usage.get("totals", {}),
                                       receipts=usage.get("receipts", []))
    if profile_roles:
        profile = dict(profile, roles=profile_roles)
        _save(bundle / "profile.json", profile)
    model_receipts = _profile_receipts(profile) or _normalize_receipts(run.get("receipts"))
    graph["profile_summary"] = {"roles": _profile_roles(profile)}
    if efficiency:
        graph["source_efficiency"] = efficiency
        _save(bundle / "source-efficiency.json", efficiency)
    recorded_status = run.get("status", "in_progress")
    lifecycle = run.get("runtime_status", recorded_status) if recorded_status in ("in_progress", "answered") else recorded_status
    status = ("in_progress" if lifecycle in ("planning", "researching", "reading", "acquiring", "auditing")
              else "answered" if lifecycle in ("complete", "completed") and graph["claims"]
              else lifecycle)
    requests = _load(source / "openalex-requests.json", _load(bundle / "openalex-requests.json", _load(bundle / "openalex-request-log.json", None)))
    graph["audit"] = {"status": "pending" if not audit else "complete" if audit_case.get("audit_complete") else "partial",
                       "model": "gpt-6-astra", "reasoning_effort": "ultra",
                       "checked_claims": sum(check.get("assessment") != "not_checked" for check in graph["source_checks"]),
                       "total_claims": len(graph["claims"]), "scope_note": audit.get("scope_note", audit_case.get("scope_note", "")),
                       "limitations": audit_case.get("limitations", []), "scores": audit_case.get("scores", {}),
                       "is_scientific_certification": False}
    run.update(kind="question", run_id=run.get("run_id", bundle.name), question=question,
               execution_mode=EXECUTION_MODE, selection_method="coverage-guided", model=run.get("model", "gpt-6.1-sol"),
               reasoning_effort="xhigh", reviewer_model="gpt-6-astra", reviewer_reasoning_effort="ultra",
               generated_at=run.get("generated_at", run.get("started_at", context.get("started_at"))),
               status=status, runtime_status=lifecycle, receipts=model_receipts,
               discovered=len(candidates), accepted=len(accepted), available_unique_sources=len(refs),
               llm_calls=sum(record.get("usage", {}).get("response_count", record.get("responses", 0))
                             for record in _profile_roles(profile).values()),
               papers_reviewed=covered, papers_core=roles.get("core", 0), papers_supporting=roles.get("supporting", 0),
               target_papers=target, preview_suspected_sources=preview_count, audit_status=graph["audit"]["status"], coverage=coverage,
               keyword_requests=sum(r.get("status") == 200 for r in requests if isinstance(r, dict)) if isinstance(requests, list) else run.get("keyword_requests"),
               search_scope="OpenAlex search across titles, abstracts, keywords and indexed full text; abstract relevance assessment followed by full-paper review.")
    access = {"accepted": accepted, "excluded": [r for r in receipts if r.get("status") not in ("downloaded_full_paper", "source_retrieved")],
              "scope_note": "Public full-paper copies; successful downloads alone do not establish full review."}
    _save_markdown(bundle / "answer.md", graph)
    _save(bundle / "answer.json", graph)
    _save(bundle / "access.json", access)
    _save(bundle / "query-net.json", plan)
    _save(bundle / "coverage.json", coverage)
    _save(bundle / "paper-selection.json", {"selection": selection, "reviewed_papers": reviewed})
    _save(bundle / "peer-review.json", {"audit": audit_case, "scope_note": audit.get("scope_note", ""), "raw_audit": audit})
    _save(bundle / "run.json", run)
    return {"run": run, "answer": graph, "access": access, "coverage": coverage}
