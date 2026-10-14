"""Run the shipped dashboard JavaScript against synthetic evidence graphs."""
import json
from pathlib import Path
import re
import subprocess


UI = Path(__file__).resolve().parents[1] / "scripts" / "dashboard" / "index.html"


def run_dashboard(tmp_path, graph, assertions):
    scripts = re.findall(r"<script>(.*?)</script>", UI.read_text(), re.S)
    assert len(scripts) == 1
    probe = tmp_path / "evidence-ui.js"
    probe.write_text(scripts[0] + "\nconst assert=require('node:assert/strict');\nconst graph=" +
                     json.dumps(graph) + ";\n" + assertions)
    result = subprocess.run(["node", str(probe)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_grouped_packets_keep_every_passage_reachable_without_overview_button_flood(tmp_path):
    evidence, passages = [], []
    for origin, count in (("E22", 76), ("E23", 81)):
        for n in range(1, count + 1):
            eid, pid = f"{origin}.{n}", f"p-{origin}-{n}"
            quote = f"Saved verbatim {eid}."
            evidence.append({"id": eid, "origin_evidence_id": origin, "paragraph_id": pid,
                             "quote": quote, "interpretation": "Synthetic interpretation."})
            passages.append({"id": pid, "source_id": "R1", "text": quote, "kind": "fulltext",
                             "start": n * 100, "end": n * 100 + len(quote)})
    graph = {"claims": [{"id": "C1", "text": "Synthetic attributed claim.", "uncertainty_score": 2,
                         "uncertainty_rationale": "Synthetic limit.",
                         "evidence_ids": [e["id"] for e in evidence]}],
             "evidence": evidence, "passages": passages,
             "references": [{"id": "R1", "paper": {"title": "Synthetic source", "year": 2026}}]}
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph), claim=graph.claims[0];
const buttons=html=>[...html.matchAll(/<button\b[^>]*data-qa-evidence="([^"]+)"[^>]*>([\s\S]*?)<\/button>/g)]
  .map(m=>({id:m[1],label:m[2]}));
for(const html of [findingHtml(graph,claim),claimInspector(graph,claim.id)]) {
  assert.deepEqual(buttons(html),[
    {id:'E22.1',label:'E22 · 76 passages'},
    {id:'E23.1',label:'E23 · 81 passages'}
  ]);
}
for(const [id,count,origin] of [['E22.1',76,'E22'],['E23.1',81,'E23']]) {
  const html=evidenceInspector(graph,id);
  const details=html.match(new RegExp('<details data-detail="evidence-packet-'+origin+'">([\\s\\S]*?)</details>'));
  assert.ok(details,'Packet passages must be in a collapsed details element');
  assert.doesNotMatch(details[0],/<details[^>]*\bopen\b/);
  assert.match(details[1],new RegExp('All '+count+' passages in '+origin));
  const links=buttons(details[1]);
  assert.deepEqual(links.map(b=>b.id),Array.from({length:count},(_,i)=>origin+'.'+(i+1)));
  for(const link of links) {
    const passageHtml=evidenceInspector(graph,link.id);
    assert.ok(passageHtml.includes('<blockquote>Saved verbatim '+link.id+'.</blockquote>'));
    assert.ok(passageHtml.includes('data-qa-context="'+link.id+'"'));
    assert.match(passageHtml,/Read in source context/);
    assert.match(passageHtml,/Synthetic source/);
  }
}
const structure=researchStructureHtml({...graph,gaps:[{description:'Synthetic gap.',evidence_ids:['E22','E23']}]});
assert.equal(buttons(structure).length,2);
assert.equal(graph.evidence.length,157);
assert.equal(JSON.stringify(graph),before,'Rendering must preserve the raw graph and all saved spans');
""")


def test_multi_reference_study_result_renders_all_citations_and_keeps_navigation(tmp_path):
    graph = {"claims": [{"id": "C1", "text": "Synthetic comparison.", "study_result_ids": ["SR1"]}],
             "study_results": [{"id": "SR1", "source_id": None, "source_ids": ["R1", "R2", "R1"],
                                "result_type": "observed_measurement",
                                "fields": {"value": {"value": "Synthetic measurement.",
                                                      "paragraph_ids": ["p1", "p2"]}}}],
             "references": [{"id": "R1", "paper": {"title": "First synthetic paper", "doi": "10.9999/first"}},
                            {"id": "R2", "paper": {"title": "Second synthetic paper", "doi": "10.9999/second"}}],
             "passages": [{"id": "p1", "source_id": "R1", "kind": "fulltext", "text": "First saved result."},
                          {"id": "p2", "source_id": "R2", "kind": "fulltext", "text": "Second saved result."}]}
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph), html=studyResultInspector(graph,'SR1');
assert.match(html,/First synthetic paper/);
assert.match(html,/Second synthetic paper/);
assert.doesNotMatch(html,/Citation not recorded/);
assert.equal((html.match(/class="citation"/g)||[]).length,2);
assert.ok(html.includes('data-qa-paper="R1"'));
assert.ok(html.includes('data-qa-paper="R2"'));
assert.ok(html.includes('data-qa-passage="p1"'));
assert.ok(html.includes('data-qa-passage="p2"'));
assert.ok(html.includes('https://doi.org/10.9999/first'));
assert.ok(html.includes('https://doi.org/10.9999/second'));
const resolved=qaStudyLinks(graph,'SR1');
assert.deepEqual(resolved.references.map(r=>r.id),['R1','R2']);
assert.equal(resolved.reference.id,'R1');
S.selected='synthetic'; S.research.synthetic={answer:graph};
global.document={getElementById:()=>null};
(async()=>{
  await inspectAnswer('study','SR1');
  assert.equal(S.inspector.sourceId,'R1');
  assert.equal(S.inspector.claimId,'C1');
  await inspectAnswer('passage','p2',{studyResultId:'SR1',fieldName:'Value'});
  assert.equal(S.inspector.sourceId,'R2');
  assert.equal(S.inspector.studyResultId,'SR1');
  assert.equal(S.inspector.claimId,'C1');
  assert.equal(S.inspector.fieldName,'Value');
  assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_run_log_separates_reviewed_papers_retrieved_sources_and_suspected_previews(tmp_path):
    run_dashboard(tmp_path, {}, r"""
const row={key:'synthetic',papers_covered:20,papers_acquired:35,papers_core:8,papers_supporting:12,
  preview_suspected_sources:2};
const html=runLogHtml([row]);
assert.match(html,/35 retrieved sources · 2 suspected previews/);
assert.match(html,/<td>20<span class="run-log-status">8 core · 12 supporting/);
assert.doesNotMatch(html,/35 full papers|35 acquired copies/);
const legacy=runLogHtml([{...row,preview_suspected_sources:null}]);
assert.match(legacy,/35 acquired copies/);
assert.doesNotMatch(legacy,/suspected previews/);
""")


def test_object_and_array_evidence_targets_render_actual_plan_descriptors(tmp_path):
    plan = {"concepts": [{"name": "capture cues", "synonyms": ["context annotation"]}],
            "queries": [{"id": "q1", "search": '"context capture" AND retrieval'}],
            "evidence_targets": {"target_full_publications": 50,
                                 "effort": {"condition": "Measured incremental capture time",
                                            "explanation": "Same observation window as benefit",
                                            "units": ["minutes", "records"]}}}
    run_dashboard(tmp_path, plan, r"""
const before=JSON.stringify(graph);
const data={query_net:graph,run:{selection_method:'coverage-guided',keyword_requests:1}};
const html=queryNetHtml(data);
assert.match(html,/target full publications: 50/);
assert.match(html,/effort: condition: Measured incremental capture time/);
assert.match(html,/Same observation window as benefit/);
assert.match(html,/minutes · records/);
assert.match(html,/context annotation/);
assert.match(html,/&quot;context capture&quot; AND retrieval/);
assert.doesNotMatch(html,/\[object Object\]|undefined/);
const array=queryNetHtml({...data,query_net:{...graph,evidence_targets:[
  'Exact comparator', {condition:'Reported added minutes',explanation:'Do not infer missing denominators'}
]}});
assert.match(array,/Exact comparator/);
assert.match(array,/Reported added minutes/);
assert.match(array,/Do not infer missing denominators/);
assert.equal(JSON.stringify(graph),before);
""")


def test_references_without_claims_keep_papers_and_source_navigation_available(tmp_path):
    graph = {"claims": [], "executive_summary": [],
             "references": [{"id": "R1", "paper": {"title": "Committed synthetic source"}}]}
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph);
const data={run:{selection_method:'coverage-guided',execution_mode:'coverage-guided full-paper research'},answer:graph};
const row={key:'synthetic',kind:'question',status:'researching'};
assert.equal(qaGraph(data),graph);
assert.equal(qaGraph({answer:{claims:[],references:[]}}),null);
const papers=modernPapersHtml(data);
assert.match(papers,/Committed synthetic source/);
assert.match(papers,/data-qa-source="R1"/);
assert.match(papers,/data-qa-paper="R1"/);
const pending=answerHtml(row,data);
assert.match(pending,/Reading selected sources/);
assert.doesNotMatch(pending,/aria-label="Executive summary"|<h2>Claims<\/h2>/);
assert.match(answerHtml({...row,status:'partial'},data),/No grounded answer is available yet/);
S.selected='synthetic';S.research.synthetic=data;
assert.equal(hasReport(row),false);
assert.equal(hasFindings(row),false);
S.sourceContexts={};
for(const scope of ['section','paper']) {
  S.sourceContexts[contextKey('R1',null,scope)]={context_label:'Saved synthetic text',paragraphs:[
    {id:'p1',text:'Saved source is immediately accessible.'}
  ]};
}
global.document={getElementById:()=>null};
(async()=>{
  await inspectAnswer('source','R1');
  assert.equal(S.inspector.sourceId,'R1');
  assert.match(sourceInspector(graph,S.inspector),/Saved source is immediately accessible/);
  await inspectAnswer('paper','R1');
  assert.equal(S.inspector.scope,'paper');
  assert.match(sourceInspector(graph,S.inspector),/Complete paper/);
  assert.match(sourceInspector(graph,S.inspector),/Committed synthetic source/);
  assert.equal(JSON.stringify(graph),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_resource_usage_preserves_unknown_rows_and_unknown_aggregates(tmp_path):
    run_dashboard(tmp_path, {}, r"""
const rows=[{key:'known',status:'done',model_calls:5,tokens:120},
  {key:'unknown',status:'partial',model_calls:null,tokens:null}];
const html=usageRowsHtml(rows);
const total=html.match(/<tr><td>Total<\/td>([\s\S]*?)<\/tr>/)[1];
assert.equal((total.match(/aria-label="Unknown"/g)||[]).length,2);
assert.doesNotMatch(total,/>5<|>120<|>0</);
assert.match(html,/<td>5<\/td><td>120<\/td>/);
assert.equal((html.match(/aria-label="Unknown"/g)||[]).length,4);
const known=usageRowsHtml([{key:'one',status:'done',model_calls:0,tokens:0},
  {key:'two',status:'done',model_calls:5,tokens:120}]);
assert.match(known,/<td>Total<\/td><td><\/td><td>5<\/td><td>120<\/td>/);
assert.match(known,/<td>0<\/td><td>0<\/td>/);
const partial=usageRowsHtml([{key:'partial',status:'done',model_calls:3,tokens:null}]);
assert.match(partial,/<td>Total<\/td><td><\/td><td>3<\/td><td><span aria-label="Unknown">—<\/span><\/td>/);
const absent=usageRowsHtml([{key:'absent',status:'done'}]);
assert.equal((absent.match(/aria-label="Unknown"/g)||[]).length,4);
""")


def test_evidence_relation_labels_preserve_contradiction_without_claiming_support(tmp_path):
    graph = {"claims": [{"id": "C1", "text": "Synthetic attributed claim.", "evidence_ids": ["E1"]}],
             "evidence": [{"id": "E1", "paragraph_id": "p1", "relation": "contradicts",
                           "quote": "The source reports the opposite result."}],
             "passages": [{"id": "p1", "source_id": "R1", "kind": "fulltext", "start": 12, "end": 56}],
             "references": [{"id": "R1", "paper": {"title": "Contradictory synthetic source"}}]}
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),html=evidenceInspector(graph,'E1');
assert.match(html,/Recorded relation: contradicts/);
assert.match(html,/<h3>Linked claims<\/h3>/);
assert.doesNotMatch(html,/Supports these claims/);
assert.match(html,/<blockquote>The source reports the opposite result\.<\/blockquote>/);
assert.match(html,/data-qa-context="E1"/);
assert.match(html,/data-qa-claim="C1"/);
for(const relation of ['supports','qualifies','contradicts','unknown']) {
  const fixture={...graph,evidence:[{...graph.evidence[0],relation}]};
  assert.ok(evidenceInspector(fixture,'E1').includes('Recorded relation: '+relation));
}
const absent={...graph,evidence:[{...graph.evidence[0],relation:null}]};
assert.match(evidenceInspector(absent,'E1'),/Recorded relation: unknown/);
assert.equal(JSON.stringify(graph),before);
""")
