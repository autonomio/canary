"""Recorded provenance survives ID collisions and broken typed result links."""
import json

import pytest

from canary.coverage_export import export_bundle
from test_coverage_evidence_ui import run_dashboard


def saved_bundle(path, investigation):
    references, spans = [], []
    for sid, rows in (("R1", [("p1", "First exact quotation."), ("p2", "Second exact quotation.")]),
                      ("R2", [("p3", "Different paper quotation."), ("p4", "Reserved suffix quotation.")])):
        text = "\n".join(quote for _, quote in rows)
        for directory in ("texts", "papers", "passages"):
            (path / directory).mkdir(exist_ok=True)
        (path / f"texts/{sid}.txt").write_text(text)
        (path / f"papers/{sid}.pdf").write_bytes(b"Synthetic saved complete body " + sid.encode())
        cursor, records = 0, []
        for pid, quote in rows:
            record = {"id": pid, "reference_id": sid, "text": quote, "start": cursor, "end": cursor + len(quote)}
            records.append(record); spans.append(record); cursor += len(quote) + 1
        (path / f"passages/{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
        references.append({"id": sid, "title": f"Saved paper {sid}", "doi": f"10.9999/{sid}",
                           "file": f"papers/{sid}.pdf", "text_file": f"texts/{sid}.txt"})
    (path / "references.json").write_text(json.dumps(references))
    (path / "investigation.json").write_text(json.dumps(investigation))
    (path / "run.json").write_text(json.dumps({"question": "Synthetic question?", "status": "complete"}))
    return export_bundle(path)["answer"]


def test_multi_passage_ids_cannot_capture_a_distinct_recorded_evidence_id(tmp_path):
    investigation = {"claims": [{"id": "C1", "text": "Multi-passage claim.", "evidence_ids": ["E1"]},
                                  {"id": "C2", "text": "Separate canonical packet claim.", "evidence_ids": ["E1.1"]},
                                  {"id": "C3", "text": "Reserved suffix claim.", "evidence_ids": ["E1.1~1"]}],
                     "evidence": [{"id": "E1", "passage_ids": ["p1", "p2"]},
                                  {"id": "E1.1", "passage_ids": ["p3"]},
                                  {"id": "E1.1~1", "passage_ids": ["p4"]}]}
    graph = saved_bundle(tmp_path, investigation)
    evidence = {r["id"]: r for r in graph["evidence"]}
    assert len(evidence) == len(graph["evidence"]) == 4
    assert graph["claims"][1]["evidence_ids"] == ["E1.1"]
    assert graph["claims"][2]["evidence_ids"] == ["E1.1~1"]
    assert evidence["E1.1"]["paragraph_id"] == "p3"
    assert evidence["E1.1~1"]["paragraph_id"] == "p4"
    assert [evidence[e]["paragraph_id"] for e in graph["claims"][0]["evidence_ids"]] == ["p1", "p2"]
    assert all(evidence[e]["origin_evidence_id"] == "E1" for e in graph["claims"][0]["evidence_ids"])
    assert export_bundle(tmp_path)["answer"]["evidence"] == graph["evidence"], "Derived IDs must remain deterministic"
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    report = (tmp_path / "answer.md").read_text().split("## C2", 1)[1].split("## C3", 1)[0]
    assert "Different paper quotation." in report and "First exact quotation." not in report
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),packet=qaEvidencePackets(graph,graph.claims[0].evidence_ids)[0];
assert.equal(packet.evidence.length,2);
assert.equal(qaEvidencePackets(graph,graph.claims[1].evidence_ids)[0].originId,'E1.1');
assert.equal(qaLinks(graph,'E1.1').reference.id,'R2');
assert.match(evidenceInspector(graph,'E1.1'),/Different paper quotation\./);
assert.doesNotMatch(evidenceInspector(graph,'E1.1'),/First exact quotation\./);
for(const e of packet.evidence) assert.equal(qaLinks(graph,e.id).reference.id,'R1');
S.selected='collision';S.research.collision={answer:graph};global.document={getElementById:()=>null};
(async()=>{
  await inspectAnswer('claim','C2');await inspectAnswer('evidence','E1.1');
  assert.equal(S.inspector.sourceId,'R2');assert.equal(S.inspector.claimId,'C2');
  assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_duplicate_study_results_retain_distinct_measurements_and_sources(tmp_path):
    investigation = {"claims": [{"id": "C1", "text": "Recorded ambiguous comparison.", "study_result_ids": ["SR1"]},
                                  {"id": "C2", "text": "Unique comparison.", "study_result_ids": ["SR2"], "evidence_ids": ["E2"]}],
                     "evidence": [{"id": "E2", "passage_ids": ["p2"]}],
                     "study_results": [{"id": "SR1", "reference_ids": ["R1"], "result_type": "measured",
                                        "fields": {"value": {"text": "11 percent", "passage_ids": ["p1"]}}},
                                       {"id": "SR1", "reference_ids": ["R2"], "result_type": "measured",
                                        "fields": {"value": {"text": "23 percent", "passage_ids": ["p3"]}}},
                                       {"id": "SR2", "reference_ids": ["R1"], "result_type": "measured",
                                        "fields": {"value": {"text": "Unique 7 percent", "passage_ids": ["p2"]}}}]}
    graph = saved_bundle(tmp_path, investigation)
    assert [r["id"] for r in graph["study_results"]] == ["SR1", "SR1", "SR2"]
    assert [r.get("study_result_occurrence") for r in graph["study_results"]] == [1, 2, None]
    assert [r["fields"]["value"]["value"] for r in graph["study_results"]] == ["11 percent", "23 percent", "Unique 7 percent"]
    issue = next(i for i in graph["binding_issues"] if i["kind"] == "duplicate_study_result_id")
    assert issue["study_result_id"] == "SR1" and issue["record_count"] == 2
    assert graph["claims"][0]["study_result_ids"] == ["SR1"]
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    report = (tmp_path / "answer.md").read_text()
    assert "Provenance failure: duplicate study result ID SR1; the linked measurement is ambiguous." in report
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),links=qaStudyLinks(graph,'SR1');
assert.equal(links.record,null);assert.equal(links.reference,null);assert.equal(links.records.length,2);
assert.match(findingHtml(graph,graph.claims[0]),/Provenance failure/);
assert.doesNotMatch(findingHtml(graph,graph.claims[1]),/Provenance failure/);
const html=studyResultInspector(graph,'SR1');assert.match(html,/Ambiguous study result ID/);
const first=html.split('data-study-occurrence="1"')[1].split('</section>')[0];
const second=html.split('data-study-occurrence="2"')[1].split('</section>')[0];
assert.match(first,/11 percent/);assert.match(first,/data-qa-passage="p1"/);assert.match(first,/10\.9999\/R1/);
assert.doesNotMatch(first,/23 percent|data-qa-passage="p3"|10\.9999\/R2/);
assert.match(second,/23 percent/);assert.match(second,/data-qa-passage="p3"/);assert.match(second,/10\.9999\/R2/);
assert.doesNotMatch(second,/11 percent|data-qa-passage="p1"|10\.9999\/R1/);
assert.equal(qaStudyLinks(graph,'SR2').record.fields.value.value,'Unique 7 percent');
S.selected='results';S.research.results={answer:graph};global.document={getElementById:()=>null};
(async()=>{
  await inspectAnswer('study','SR1',{claimId:'C1'});assert.equal(S.inspector.sourceId,undefined);
  assert.equal(S.inspector.studyResultId,'SR1');assert.equal(S.inspector.claimId,'C1');
  await inspectAnswer('passage','p3',{studyResultId:'SR1',fieldName:'Value'});
  assert.equal(S.inspector.sourceId,'R2');assert.equal(S.inspector.studyResultId,'SR1');
  await inspectAnswer('study','SR2',{claimId:'C2'});assert.equal(S.inspector.sourceId,'R1');
  assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_unknown_study_result_links_are_retained_and_occurrence_scoped(tmp_path):
    investigation = {"claims": [{"id": "C1", "text": "Affected recorded occurrence.", "study_result_ids": ["SR_missing"]},
                                  {"id": "C1", "text": "Clean recorded occurrence.", "study_result_ids": []},
                                  {"id": "C2", "text": "Unaffected unique claim.", "study_result_ids": []}]}
    graph = saved_bundle(tmp_path, investigation)
    missing = [i for i in graph["binding_issues"] if i["kind"] == "missing_study_result"]
    assert missing == [{"kind": "missing_study_result", "claim_id": "C1", "study_result_id": "SR_missing", "claim_occurrence": 1}]
    assert graph["claims"][0]["study_result_ids"] == ["SR_missing"]
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    report = (tmp_path / "answer.md").read_text()
    first, remainder = report.split("## C1 · recorded occurrence 1", 1)[1].split("## C1 · recorded occurrence 2", 1)
    assert "Provenance failure: missing study result SR_missing" in first
    assert "missing study result" not in remainder
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph);
assert.ok(qaClaimBindingIssues(graph,graph.claims[0]).some(i=>i.kind==='missing_study_result'));
assert.ok(!qaClaimBindingIssues(graph,graph.claims[1]).some(i=>i.kind==='missing_study_result'));
assert.match(findingHtml(graph,graph.claims[0]),/data-qa-study="SR_missing"/);
assert.match(claimInspector(graph,'C1'),/No saved study-result record matches this ID/);
assert.match(studyResultInspector(graph,'SR_missing'),/Provenance failure: study result not recorded/);
assert.doesNotMatch(studyResultInspector(graph,'SR_missing'),/data-qa-passage=/);
S.selected='missing';S.research.missing={answer:graph};global.document={getElementById:()=>null};
(async()=>{
  await inspectAnswer('claim','C1');await inspectAnswer('study','SR_missing',{claimId:'C1'});
  assert.equal(S.inspector.kind,'study');assert.equal(S.inspector.studyResultId,'SR_missing');
  assert.equal(S.inspector.sourceId,undefined);assert.equal(S.inspector.claimId,'C1');
  assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


@pytest.mark.parametrize("first_passages", [["p1"], ["p1", "p2"]])
def test_duplicate_evidence_keeps_all_occurrences_with_distinct_navigation(tmp_path, first_passages):
    investigation = {"claims": [{"id": "C1", "text": "Ambiguous ID-only evidence link.", "evidence_ids": ["E_same"]},
                                  {"id": "C2", "text": "Unique evidence link.", "evidence_ids": ["E_unique"]}],
                     "evidence": [{"id": "E_same", "passage_ids": first_passages, "interpretation": "First recorded interpretation.", "relation": "supports"},
                                  {"id": "E_same", "passage_ids": ["p3"], "interpretation": "Second recorded interpretation.", "relation": "contradicts"},
                                  {"id": "E_unique", "passage_ids": ["p4"], "relation": "supports"}]}
    graph = saved_bundle(tmp_path, investigation)
    duplicate = [e for e in graph["evidence"] if e["origin_evidence_id"] == "E_same"]
    assert graph["claims"][0]["evidence_ids"] == ["E_same"], "An ambiguous ID must not be assigned to one packet"
    assert len(duplicate) == len(first_passages) + 1
    assert [e["evidence_occurrence"] for e in duplicate] == [1] * len(first_passages) + [2]
    assert len({e["navigation_id"] for e in duplicate}) == len(duplicate)
    if len(first_passages) == 1:
        assert [e["id"] for e in duplicate] == ["E_same", "E_same"], "Canonical IDs must survive"
    assert graph["binding_issues"][0]["kind"] == "duplicate_evidence_id"
    assert graph["binding_issues"][0]["record_count"] == 2
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    report = (tmp_path / "answer.md").read_text().split("## C1", 1)[1].split("## C2", 1)[0]
    assert "Provenance failure: duplicate evidence ID E_same" in report
    assert "recorded occurrence 1" in report and "recorded occurrence 2" in report
    assert "First exact quotation." in report and "Different paper quotation." in report
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),links=qaLinks(graph,'E_same');
assert.equal(links.ambiguous,true);assert.equal(links.evidence,null);assert.equal(links.reference,null);
assert.match(findingHtml(graph,graph.claims[0]),/Provenance failure/);
assert.doesNotMatch(findingHtml(graph,graph.claims[1]),/Provenance failure/);
assert.equal(qaEvidencePackets(graph,graph.claims[0].evidence_ids)[0].id,'E_same');
const html=evidenceInspector(graph,'E_same');assert.match(html,/Ambiguous evidence ID/);
const first=html.split('data-evidence-occurrence="1"')[1].split('</section>')[0];
const second=html.split('data-evidence-occurrence="2"')[1].split('</section>')[0];
assert.match(first,/First exact quotation\./);assert.doesNotMatch(first,/Different paper quotation\./);
assert.match(second,/Different paper quotation\./);assert.doesNotMatch(second,/First exact quotation\./);
const occurrences=graph.evidence.filter(e=>e.origin_evidence_id==='E_same');
for(const e of occurrences) {const x=qaLinks(graph,e.navigation_id);assert.equal(x.ambiguous,false);assert.equal(x.evidence,e);assert.equal(x.reference.id,e.evidence_occurrence===1 ? 'R1':'R2');}
S.selected='occurrences';S.research.occurrences={answer:graph};global.document={getElementById:()=>null};
(async()=>{
  await inspectAnswer('evidence','E_same');assert.equal(S.inspector.sourceId,undefined);
  for(const e of occurrences) {await inspectAnswer('evidence',e.navigation_id);assert.equal(S.inspector.sourceId,e.evidence_occurrence===1 ? 'R1':'R2');}
  assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_unknown_typed_evidence_links_never_capture_generated_passages(tmp_path):
    investigation = {"claims": [{"id": "C1", "text": "Actual packet claim.", "evidence_ids": ["E1"]},
                                  {"id": "C2", "text": "Missing packet claim.", "evidence_ids": ["E1.1"]}],
                     "evidence": [{"id": "E1", "passage_ids": ["p1", "p2"]}],
                     "gaps": [{"id": "G1", "description": "Recorded gap.", "evidence_ids": ["E1.2"]}],
                     "residue": [{"id": "Z1", "observation": "Recorded residue.", "evidence_ids": ["E1.1~1"]}],
                     "contradictions": [{"id": "D1", "description": "Recorded contradiction.", "evidence_ids": ["E1.2~1"]}]}
    graph = saved_bundle(tmp_path, investigation)
    missing_ids = {"E1.1", "E1.2", "E1.1~1", "E1.2~1"}
    assert not missing_ids.intersection(e["id"] for e in graph["evidence"])
    assert graph["claims"][1]["evidence_ids"] == ["E1.1"]
    assert any(i["kind"] == "missing_evidence" and i["claim_id"] == "C2" for i in graph["binding_issues"])
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    report = (tmp_path / "answer.md").read_text().split("## C2", 1)[1].split("## Contradictions", 1)[0]
    assert "Provenance failure: missing evidence E1.1" in report
    assert "exact quotation" not in report.lower()
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph);
assert.equal(qaLinks(graph,undefined).evidence,null);assert.equal(qaLinks(graph,null).evidence,null);
const stale={...graph,evidence:[...graph.evidence,{id:'E1.1',origin_evidence_id:'E1',paragraph_id:'p1',quote:'First exact quotation.'}]};
assert.equal(qaLinks(stale,'E1.1').evidence,null,'A recorded missing ID must not resolve to an old derived-ID collision');
assert.doesNotMatch(evidenceInspector(stale,'E1.1'),/<blockquote>|data-qa-context=/);
for(const id of ['E1.1','E1.2','E1.1~1','E1.2~1']) {
 assert.equal(qaLinks(graph,id).evidence,null);assert.equal(qaLinks(graph,id).reference,null);
 assert.match(evidenceInspector(graph,id),/Provenance failure: evidence not recorded/);
 assert.doesNotMatch(evidenceInspector(graph,id),/<blockquote>|data-qa-context=/);
}
assert.match(findingHtml(graph,graph.claims[1]),/Provenance failure/);
S.selected='missing-evidence';S.research['missing-evidence']={answer:graph};global.document={getElementById:()=>null};
(async()=>{
 for(const id of ['E1.1','E1.2','E1.1~1','E1.2~1']) {await inspectAnswer('evidence',id);assert.equal(S.inspector.evidenceId,id);assert.equal(S.inspector.sourceId,undefined);}
 assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_nullable_study_fields_preserve_committed_references_and_saved_papers(tmp_path):
    investigation = {"claims": [{"id": "C1", "text": "Recorded qualitative result.", "study_result_ids": ["SR1"]}],
                     "study_results": [{"id": "SR1", "reference_ids": ["R2"], "result_type": "qualitative",
                                        "fields": {"value": None, "reported_uncertainty": None}}]}
    graph = saved_bundle(tmp_path, investigation)
    assert graph["passages"] == []
    assert [r["id"] for r in graph["references"]] == ["R2"]
    assert graph["references"][0]["paper"]["extra"]["local_file"] == "papers/R2.pdf"
    assert (tmp_path / "papers/R2.txt").is_file()
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    assert "R2 · Saved paper R2 · 10.9999/R2" in (tmp_path / "answer.md").read_text()
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),x=qaStudyLinks(graph,'SR1');assert.equal(x.reference.id,'R2');
const html=studyResultInspector(graph,'SR1');assert.match(html,/10\.9999\/R2/);assert.match(html,/Not extracted/);assert.match(html,/data-qa-paper="R2"/);
S.selected='nullable';S.research.nullable={answer:graph};global.document={getElementById:()=>null};
S.sourceContexts={};S.sourceContexts[contextKey('R2',null,'paper')]={paragraphs:[],context_label:'Saved paper'};
global.fetch=()=>{throw new Error('The saved paper context is already committed');};
(async()=>{
 await inspectAnswer('study','SR1');assert.equal(S.inspector.sourceId,'R2');
 await inspectAnswer('paper','R2');assert.equal(S.inspector.sourceId,'R2');assert.equal(S.inspector.scope,'paper');
 assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_unknown_summary_claim_retains_assertion_and_opens_failure_inspector(tmp_path):
    investigation = {"executive_summary": [{"text": "Recorded summary with broken provenance.", "claim_ids": ["C1", "C_missing"]},
                                            {"text": "Intact summary.", "claim_ids": ["C1"]}],
                     "claims": [{"id": "C1", "text": "Existing recorded claim.", "evidence_ids": ["E1"]}],
                     "evidence": [{"id": "E1", "passage_ids": ["p1"]}]}
    graph = saved_bundle(tmp_path, investigation)
    assert graph["executive_summary"] == investigation["executive_summary"]
    assert [i for i in graph["binding_issues"] if i["kind"] == "missing_summary_claim"] == [
        {"kind": "missing_summary_claim", "summary_index": 1, "claim_id": "C_missing"}]
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    assert "Provenance failure: executive summary links missing claim C_missing" in (tmp_path / "answer.md").read_text()
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),html=answerHtml({key:'summary',kind:'question',status:'answered'},{answer:graph});
assert.match(html,/Recorded summary with broken provenance\./);assert.match(html,/data-qa-claim="C_missing"/);
assert.equal((html.match(/Provenance failure · missing claim link/g)||[]).length,1);
const missing=claimInspector(graph,'C_missing');assert.match(missing,/Provenance failure: claim not recorded/);
assert.match(missing,/Recorded summary with broken provenance/);assert.doesNotMatch(missing,/data-qa-evidence=/);
assert.match(claimInspector(graph,'C1'),/Existing recorded claim/);
S.selected='summary';S.research.summary={answer:graph};global.document={getElementById:()=>null};
(async()=>{
 await inspectAnswer('claim','C_missing');assert.equal(S.inspector.claimId,'C_missing');assert.equal(S.inspector.kind,'claim');
 await inspectAnswer('claim','C1');assert.equal(S.inspector.claimId,'C1');assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


@pytest.mark.parametrize("section", ["gaps", "contradictions", "residue"])
def test_missing_context_claim_links_keep_recorded_text_and_open_failure(tmp_path, section):
    broken = {"id": "X1", "description": "Recorded gap or contradiction.", "observation": "Recorded residue.",
              "claim_ids": ["C1", "C_missing"], "evidence_ids": ["E1"]}
    intact = {"id": "X2", "description": "Intact context.", "claim_ids": ["C1"], "evidence_ids": ["E1"]}
    investigation = {"claims": [{"id": "C1", "text": "Existing bound claim.", "evidence_ids": ["E1"]}],
                     "evidence": [{"id": "E1", "passage_ids": ["p1"]}], section: [broken, intact]}
    graph = saved_bundle(tmp_path, investigation)
    issues = [i for i in graph["binding_issues"] if i["kind"] == "missing_linked_claim"]
    assert issues == [{"kind": "missing_linked_claim", "section": section, "record_id": "X1", "record_index": 1, "claim_id": "C_missing"}]
    assert graph[section] == [broken, intact]
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    report = (tmp_path / "answer.md").read_text()
    assert f"Provenance failure: missing claim C_missing in {section} record X1." in report
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),html=researchStructureHtml(graph);
assert.match(html,/data-qa-claim="C_missing"/);assert.match(html,/Provenance failure · missing claim link/);
assert.equal((html.match(/Provenance failure · missing claim link/g)||[]).length,1);
const missing=claimInspector(graph,'C_missing');assert.match(missing,/Provenance failure: claim not recorded/);
assert.match(missing,/Linked (gaps|contradictions|residue) · X1/);assert.match(missing,/Recorded gap or contradiction/);
assert.doesNotMatch(missing,/data-qa-evidence=/);
S.selected='context';S.research.context={answer:graph};global.document={getElementById:()=>null};
(async()=>{
 await inspectAnswer('claim','C_missing');assert.equal(S.inspector.kind,'claim');assert.equal(S.inspector.claimId,'C_missing');
 await inspectAnswer('claim','C1');assert.equal(S.inspector.claimId,'C1');
 assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_evidence_empty_claims_warn_without_gating_or_changing_recorded_occurrences(tmp_path):
    claims = [{"id": "C1", "text": "Unbound first occurrence.", "evidence_ids": []},
              {"id": "C1", "text": "Bound second occurrence.", "evidence_ids": ["E1"]},
              {"id": "C2", "text": "Unbound unique claim.", "evidence_ids": []},
              {"id": "C3", "text": "Intact bound claim.", "evidence_ids": ["E1"]}]
    investigation = {"claims": claims, "evidence": [{"id": "E1", "passage_ids": ["p1"]}]}
    graph = saved_bundle(tmp_path, investigation)
    assert [i for i in graph["binding_issues"] if i["kind"] == "lacks_evidence"] == [
        {"kind": "lacks_evidence", "claim_id": "C1", "claim_occurrence": 1},
        {"kind": "lacks_evidence", "claim_id": "C2"}]
    assert [(c["id"], c["text"], c["evidence_ids"]) for c in graph["claims"]] == [(c["id"], c["text"], c["evidence_ids"]) for c in claims]
    assert json.loads((tmp_path / "investigation.json").read_text()) == investigation
    assert json.loads((tmp_path / "run.json").read_text())["status"] == "answered"
    report = (tmp_path / "answer.md").read_text()
    first, remainder = report.split("## C1 · recorded occurrence 1", 1)[1].split("## C1 · recorded occurrence 2", 1)
    second, remainder = remainder.split("## C2", 1)
    unique, intact = remainder.split("## C3", 1)
    assert "Provenance failure: this claim has no recorded evidence IDs." in first and "exact quotation" not in first.lower()
    assert "no recorded evidence IDs" not in second and "First exact quotation." in second
    assert "no recorded evidence IDs" in unique and "no recorded evidence IDs" not in intact
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),lacks=c=>qaClaimBindingIssues(graph,c).some(i=>i.kind==='lacks_evidence');
assert.deepEqual(graph.claims.map(lacks),[true,false,true,false]);
assert.match(findingHtml(graph,graph.claims[2]),/Provenance failure · details/);
assert.doesNotMatch(findingHtml(graph,graph.claims[3]),/Provenance failure/);
const inspector=claimInspector(graph,'C1'),one=inspector.split('data-claim-occurrence="1"')[1].split('</section>')[0],two=inspector.split('data-claim-occurrence="2"')[1].split('</section>')[0];
assert.match(one,/This claim has no recorded evidence IDs\./);assert.doesNotMatch(two,/no recorded evidence IDs/);
const stale={...graph,binding_issues:[]};assert.ok(qaClaimBindingIssues(stale,stale.claims[2]).some(i=>i.kind==='lacks_evidence'));
S.selected='unbound';S.research.unbound={answer:graph};global.document={getElementById:()=>null};
(async()=>{
 await inspectAnswer('claim','C2');assert.equal(S.inspector.claimId,'C2');
 assert.match(claimInspector(graph,'C2'),/Unbound unique claim/);assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
