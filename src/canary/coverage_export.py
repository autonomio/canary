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

from .runs import has_native_findings

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


def _typed_links(value, key):
    """Read every typed link in nested committed records, including broken links."""
    if isinstance(value, dict):
        yield from _unique(value.get(key, []))
        for child in value.values():
            yield from _typed_links(child, key)
    elif isinstance(value, list):
        for child in value:
            yield from _typed_links(child, key)


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


def _profile_response_count(profile: dict) -> int | None:
    """Count recorded native responses only when every present role is known."""
    roles = _profile_roles(profile)
    if not roles:
        return None
    counts = []
    for record in roles.values():
        if not isinstance(record, dict):
            return None
        usage = record.get("usage", {})
        usage = usage if isinstance(usage, dict) else {}
        if usage.get("available") is False or record.get("available") is False:
            return None
        count = usage.get("response_count", record.get("responses"))
        if type(count) is not int or count < 0:
            return None
        counts.append(count)
    return sum(counts)


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
    if not has_native_findings(graph):
        path.unlink(missing_ok=True)  # Remove a stale derived stub, never source/research records.
        return
    lines = ["# " + str(graph.get("question", "?")), "", str(graph.get("scope_note", "")), ""]
    for summary_index, summary in enumerate(graph.get("executive_summary", []), 1):
        if isinstance(summary, str):
            lines.extend([summary, ""])
        elif isinstance(summary, dict):
            lines.extend([str(summary.get("text", "")), "Claims: " + ", ".join(summary.get("claim_ids", [])), ""])
        for issue in _rows(graph.get("binding_issues")):
            if issue.get("kind") == "lacks_claim_binding" and issue.get("summary_index") == summary_index:
                lines.extend(["Provenance failure: this executive assertion has no recorded claim IDs.", ""])
            elif issue.get("kind") == "missing_summary_claim" and issue.get("summary_index") == summary_index:
                lines.extend(["Provenance failure: executive summary links missing claim " + str(issue.get("claim_id")), ""])
    checks = {r.get("claim_id"): r for r in _rows(graph.get("source_checks"))}
    evidence = _rows(graph.get("evidence"))
    passages = {r.get("id"): r for r in _rows(graph.get("passages"))}
    for claim in _rows(graph.get("claims")):
        label = str(claim["id"]) + (f" · recorded occurrence {claim['claim_occurrence']}" if "claim_occurrence" in claim else "")
        lines.extend(["## " + label, "", str(claim.get("text", "")), "",
                      f'Uncertainty: {claim.get("uncertainty_class", "unknown")} ({claim.get("uncertainty_score", "unknown")}/3; ordinal).',
                      str(claim.get("uncertainty_rationale", "")), "Audit: " + str(checks.get(claim["id"], {}).get("assessment", "not_checked")), ""])
        for issue in _rows(graph.get("binding_issues")):
            linked = issue.get("claim_id") == claim["id"] or (
                not issue.get("claim_id") and (issue.get("study_result_id") in claim.get("study_result_ids", []) or
                issue.get("evidence_id") in claim.get("evidence_ids", [])))
            if not linked or issue.get("claim_occurrence", claim.get("claim_occurrence")) != claim.get("claim_occurrence"):
                continue
            if issue.get("kind") == "lacks_verbatim":
                occurrence = f" · recorded occurrence {issue['evidence_occurrence']}" if "evidence_occurrence" in issue else ""
                lines.extend([f"Provenance failure: evidence {issue.get('evidence_id')}{occurrence} has no recorded verbatim passages.", ""])
            elif issue.get("kind") == "uncertainty_mismatch":
                lines.extend([f"Recorded-data inconsistency: uncertainty class {issue.get('recorded_class')} conflicts with score {issue.get('recorded_score')}/3; both values are retained.", ""])
            elif issue.get("kind") == "lacks_evidence":
                lines.extend(["Provenance failure: this claim has no recorded evidence IDs.", ""])
            elif issue.get("kind") == "missing_evidence":
                lines.extend(["Provenance failure: missing evidence " + str(issue.get("evidence_id")), ""])
            elif issue.get("kind") == "duplicate_evidence_id":
                lines.extend(["Provenance failure: duplicate evidence ID " + str(issue.get("evidence_id")) + "; the ID-only link cannot identify one recorded packet.", ""])
            elif issue.get("kind") == "duplicate_claim_id":
                lines.extend(["Provenance failure: duplicate claim ID " + str(claim["id"]) + "; ID-only audit checks are not assigned.", ""])
            elif issue.get("kind") == "missing_study_result":
                lines.extend(["Provenance failure: missing study result " + str(issue.get("study_result_id")), ""])
            elif issue.get("kind") == "duplicate_study_result_id":
                lines.extend(["Provenance failure: duplicate study result ID " + str(issue.get("study_result_id")) + "; the linked measurement is ambiguous.", ""])
        for eid in claim.get("evidence_ids", []):
            ambiguous = any(i.get("kind") == "duplicate_evidence_id" and i.get("evidence_id") == eid
                            for i in graph.get("binding_issues", []))
            items = [r for r in evidence if (r.get("origin_evidence_id") == eid if ambiguous else
                                             r.get("navigation_id", r.get("id")) == eid)]
            for item in items:
                if item.get("verbatim_status") == "missing":
                    continue  # The metadata-only packet has no quotation or source attribution.
                span = passages.get(item.get("paragraph_id"), {})
                occurrence = f" · recorded occurrence {item['evidence_occurrence']}" if "evidence_occurrence" in item else ""
                lines.extend([f'Evidence {eid}{occurrence} · {item.get("relation", "supports")} · {span.get("source_id", "?")} / {item.get("paragraph_id", "?")}',
                              str(item.get("interpretation", "")), "", *["> " + line for line in str(item.get("quote", "")).splitlines()], ""])
    for key, label in (("contradictions", "Contradictions"), ("gaps", "Gaps"), ("residue", "Residue"), ("next_questions", "Next questions")):
        rows = _rows(graph.get(key))
        if rows:
            lines.extend(["## " + label, ""])
            for record_index, row in enumerate(rows, 1):
                lines.extend([str(row.get("description", row.get("observation", row.get("question", "")))),
                              str(row.get("why_it_matters", row.get("justification", ""))), ""])
                for issue in _rows(graph.get("binding_issues")):
                    if issue.get("kind") == "missing_linked_claim" and issue.get("section") == key and issue.get("record_index") == record_index:
                        lines.extend([f"Provenance failure: missing claim {issue.get('claim_id')} in {key} record {row.get('id', record_index)}.", ""])
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
    accounting = payload.get("accounting", {})
    accounting = accounting if isinstance(accounting, dict) else {}
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
    raw_evidence = _rows(investigation.get("evidence"))
    evidence_counts = Counter(e.get("id") for e in raw_evidence)
    duplicate_evidence_ids = {key for key, count in evidence_counts.items() if count > 1}
    for identifier, count in evidence_counts.items():
        if count > 1:
            graph["binding_issues"].append({"kind": "duplicate_evidence_id", "evidence_id": identifier,
                                            "record_count": count,
                                            "reason": "Distinct evidence packets share this ID; an ID-only link cannot select one occurrence."})
    # Unknown typed links are reserved too: generating their ID must not repair them.
    reserved_evidence_ids = set(evidence_counts) | set(_typed_links(investigation, "evidence_ids")) | set(_typed_links(efficiency, "evidence_ids"))
    allocated_evidence_ids = set()

    def derived_id(base):
        identifier, suffix = base, 0
        while identifier in reserved_evidence_ids or identifier in allocated_evidence_ids:
            suffix += 1
            identifier = f"{base}~{suffix}"
        allocated_evidence_ids.add(identifier)
        return identifier

    evidence_occurrences = Counter()
    for evidence in raw_evidence:
        identifier = evidence["id"]
        evidence_occurrences[identifier] += 1
        occurrence = evidence_occurrences[identifier]
        ambiguous = identifier in duplicate_evidence_ids
        identifiers = _unique(evidence.get("passage_ids", []))
        evidence_map.setdefault(identifier, [identifier] if ambiguous else [])
        if not identifiers:
            issue = {"kind": "lacks_verbatim", "evidence_id": identifier}
            item = {"id": identifier, "origin_evidence_id": identifier, "paragraph_id": None,
                    "passage_ids": [], "verbatim_status": "missing",
                    "interpretation": evidence.get("interpretation", ""), "relation": evidence.get("relation")}
            if ambiguous:
                issue["evidence_occurrence"] = occurrence
                item.update(evidence_occurrence=occurrence, navigation_id=derived_id(f"{identifier}~occurrence{occurrence}.1"))
            graph["binding_issues"].append(issue)
            graph["evidence"].append(item)
            if not ambiguous:
                evidence_map[identifier].append(identifier)
        for n, pid in enumerate(identifiers, 1):
            span = spans.get(pid)
            if span is None:
                graph["binding_issues"].append({"kind": "missing_passage", "evidence_id": identifier, "passage_id": pid})
                continue
            eid = identifier if len(identifiers) == 1 else derived_id(f"{identifier}.{n}")
            item = {"id": eid, "origin_evidence_id": identifier, "paragraph_id": pid,
                    "quote": span.get("text", ""), "interpretation": evidence.get("interpretation", ""),
                    "relation": evidence.get("relation", "supports")}
            if ambiguous:
                item.update(evidence_occurrence=occurrence, navigation_id=derived_id(f"{identifier}~occurrence{occurrence}.{n}"))
            graph["evidence"].append(item)
            if not ambiguous:
                evidence_map[identifier].append(eid)
            used_passages.add(pid)
    raw_study_results = _rows(investigation.get("study_results"))
    study_counts = Counter(r.get("id") for r in raw_study_results)
    for identifier, count in study_counts.items():
        if count > 1:
            graph["binding_issues"].append({"kind": "duplicate_study_result_id", "study_result_id": identifier,
                                            "record_count": count,
                                            "reason": "Distinct recorded study results share this ID; the linked measurement is ambiguous."})
    raw_claims = _rows(investigation.get("claims"))
    claim_counts = Counter(c.get("id") for c in raw_claims)
    for summary_index, summary in enumerate(graph["executive_summary"], 1):
        text = summary if isinstance(summary, str) else summary.get("text", "") if isinstance(summary, dict) else ""
        links = _unique(summary.get("claim_ids", [])) if isinstance(summary, dict) else []
        if isinstance(text, str) and text.strip() and not links:
            graph["binding_issues"].append({"kind": "lacks_claim_binding", "summary_index": summary_index})
        if isinstance(summary, dict):
            for identifier in _unique(summary.get("claim_ids", [])):
                if identifier not in claim_counts:
                    graph["binding_issues"].append({"kind": "missing_summary_claim", "summary_index": summary_index, "claim_id": identifier})
    for section in ("gaps", "contradictions", "residue"):
        for record_index, record in enumerate(_rows(graph.get(section)), 1):
            for identifier in _unique(record.get("claim_ids", [])):
                if identifier not in claim_counts:
                    graph["binding_issues"].append({"kind": "missing_linked_claim", "section": section,
                                                    "record_id": record.get("id"), "record_index": record_index,
                                                    "claim_id": identifier})
    duplicate_claim_ids = {identifier for identifier, count in claim_counts.items() if count > 1}
    for identifier, count in claim_counts.items():
        if count > 1:
            graph["binding_issues"].append({"kind": "duplicate_claim_id", "claim_id": identifier,
                                            "record_count": count,
                                            "reason": "The recorded ID identifies distinct claims; ID-only links and audit checks are ambiguous."})
    occurrences = Counter()
    for claim in raw_claims:
        identifier = claim.get("id")
        occurrences[identifier] += 1
        occurrence = occurrences[identifier]
        if not _unique(claim.get("evidence_ids", [])):
            issue = {"kind": "lacks_evidence", "claim_id": identifier}
            if identifier in duplicate_claim_ids:
                issue["claim_occurrence"] = occurrence
            graph["binding_issues"].append(issue)
        for key in _unique(claim.get("evidence_ids", [])):
            if key not in evidence_map:
                issue = {"kind": "missing_evidence", "claim_id": identifier, "evidence_id": key}
                if identifier in duplicate_claim_ids:
                    issue["claim_occurrence"] = occurrence
                graph["binding_issues"].append(issue)
        for key in _unique(claim.get("study_result_ids", [])):
            if key not in study_counts:
                issue = {"kind": "missing_study_result", "claim_id": identifier, "study_result_id": key}
                if identifier in duplicate_claim_ids:
                    issue["claim_occurrence"] = occurrence
                graph["binding_issues"].append(issue)
        u = claim.get("uncertainty", {})
        expected_score = {"low": 1, "moderate": 2, "high": 3}.get(u.get("class"))
        if expected_score is not None and type(u.get("score")) is int and u.get("score") != expected_score:
            issue = {"kind": "uncertainty_mismatch", "claim_id": identifier,
                     "recorded_class": u.get("class"), "recorded_score": u.get("score")}
            if identifier in duplicate_claim_ids:
                issue["claim_occurrence"] = occurrence
            graph["binding_issues"].append(issue)
        record = dict(claim, uncertainty_class=u.get("class"), uncertainty_score=u.get("score"),
                      uncertainty_rationale=u.get("rationale", ""),
                      evidence_ids=[eid for key in claim.get("evidence_ids", []) for eid in (evidence_map.get(key) or [key])])
        if identifier in duplicate_claim_ids:
            record["claim_occurrence"] = occurrence
        graph["claims"].append(record)
    field_map = {"system": "setup", "intervention": "intervention", "comparator": "comparator",
                 "conditions": "conditions", "endpoint": "outcome", "value": "value", "units": "units",
                 "sample_size": "sample_size", "reported_uncertainty": "uncertainty"}
    type_map = {"measured": "observed_measurement", "fitted": "fitted_estimate", "simulated": "predicted_contrast"}
    study_occurrences = Counter()
    for record in raw_study_results:
        study_occurrences[record.get("id")] += 1
        fields = {}
        for name, f in record.get("fields", {}).items():
            if isinstance(f, dict):
                ids = _unique(f.get("passage_ids", []))
                fields[field_map.get(name, name)] = {"value": f.get("text"), "paragraph_ids": ids}
                used_passages.update(ids)
        sources = _unique(record.get("reference_ids", []))
        exported = dict(record, source_id=sources[0] if len(sources) == 1 else None,
                        source_ids=sources, fields=fields,
                        result_type=type_map.get(record.get("result_type"), record.get("result_type")))
        if study_counts[record.get("id")] > 1:
            exported["study_result_occurrence"] = study_occurrences[record.get("id")]
        graph["study_results"].append(exported)
    audit = _load(bundle / "audit" / "audit.json", _load(bundle / "audit.json", {}))
    audit = audit if isinstance(audit, dict) else {}
    audit_case = _audit_record(audit)
    checks = {c.get("claim_id"): c for c in _rows(audit_case.get("claim_checks"))}
    for claim in graph["claims"]:
        ambiguous = claim["id"] in duplicate_claim_ids
        check = {} if ambiguous else checks.get(claim["id"], {})
        assessment = check.get("assessment", "not_checked")
        # "Corrected" can describe a proposed correction, not a published rewrite.
        normalized = {"correction_needed": "correction_needed", "partly_supported": "unresolved"}.get(assessment, assessment)
        ids = _unique(check.get("passage_ids", check.get("paragraph_ids", [])))
        used_passages.update(ids)
        bound_check = dict(check, claim_id=claim["id"], assessment=normalized,
                           raw_assessment=assessment, paragraph_ids=ids,
                           reason=("Duplicate claim ID; the ID-only audit record cannot be assigned to this occurrence."
                                   if ambiguous else check.get("reason", "Audit pending." if not audit else "Not checked in the recorded audit scope.")),
                           correction_applied=False)
        if ambiguous:
            bound_check["claim_occurrence"] = claim["claim_occurrence"]
        graph["source_checks"].append(bound_check)
    used_sources = set(admitted) | {sid for result in raw_study_results for sid in _unique(result.get("reference_ids", []))}
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
                "scarcity_statement": accounting.get("scarcity_statement"),
                "scarcity_independently_certified": accounting.get("scarcity_independently_certified", False),
                "counts_scope": "Reviewed papers require selected admission, core/supporting eligibility, read passages and a substantive review note. Related reports may share participants or studies; paper count is not independent study count."}
    search_progress = _load(source / "search-progress.json", _load(bundle / "search-progress.json", None))
    if isinstance(search_progress, dict):
        coverage["search_progress"] = search_progress
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
    has_findings = has_native_findings(graph)
    status = ("in_progress" if lifecycle in ("planning", "researching", "reading", "acquiring", "auditing")
              else "answered" if lifecycle in ("complete", "completed") and has_findings
              else "complete" if lifecycle == "answered" and not has_findings else lifecycle)
    requests = _load(source / "openalex-requests.json", _load(bundle / "openalex-requests.json", _load(bundle / "openalex-request-log.json", None)))
    graph["audit"] = {"status": "pending" if not audit else "complete" if audit_case.get("audit_complete") and not duplicate_claim_ids else "partial",
                       "model": "gpt-6-astra", "reasoning_effort": "ultra",
                       "checked_claims": sum(check.get("assessment") != "not_checked" for check in graph["source_checks"]),
                       "total_claims": len(graph["claims"]), "scope_note": audit.get("scope_note", audit_case.get("scope_note", "")),
                       "limitations": audit_case.get("limitations", []), "scores": audit_case.get("scores", {}),
                       "is_scientific_certification": False}
    run.update(kind="question", run_id=run.get("run_id", bundle.name), question=question,
               execution_mode=EXECUTION_MODE, selection_method="coverage-guided", model=run.get("model", "gpt-6.1-sol"),
               reasoning_effort="xhigh", reviewer_model="gpt-6-astra", reviewer_reasoning_effort="ultra",
               generated_at=run.get("generated_at", run.get("started_at", context.get("started_at"))),
               status=status, runtime_status=lifecycle, answered=has_findings, receipts=model_receipts,
               discovered=len(candidates), accepted=len(accepted), available_unique_sources=len(refs),
               llm_calls=_profile_response_count(profile),
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
