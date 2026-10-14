"""Recorded provenance survives ID collisions and broken typed result links."""
import json

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
                                  {"id": "C2", "text": "Unique comparison.", "study_result_ids": ["SR2"]}],
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
