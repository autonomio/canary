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


def test_service_details_show_the_api_reported_shared_pdf_capacity(tmp_path):
    run_dashboard(tmp_path, {}, r'''
const element={innerHTML:''};
global.document={getElementById:()=>element};
const status={data_directory:'/isolated/service',pdf_extraction:{engine:'pdftotext',concurrent_pdf_conversions:1,skip_superseded_copies:true}};
global.fetch=async path=>{assert.equal(path,'/api/status');return {ok:true,json:async()=>status};};
(async()=>{
  await refreshService();
  assert.match(element.innerHTML,/pdftotext/);
  assert.match(element.innerHTML,/1 shared conversion slot; downloads remain concurrent/);
  assert.match(element.innerHTML,/Queued alternate copies skipped after a usable copy is found/);
  delete status.pdf_extraction.concurrent_pdf_conversions;
  delete status.pdf_extraction.skip_superseded_copies;
  await refreshService();
  assert.doesNotMatch(element.innerHTML,/shared conversion slot|Queued alternate copies/);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_usage_and_findings_freshness_are_distinct_in_receipt_details(tmp_path):
    run_dashboard(tmp_path, {}, r'''
const html=coverageTokenHtml({run:{selection_method:'coverage-guided'},profile:{roles:{},progress:{
  updated_at:'2026-10-29T08:20:00Z', findings_updated_at:'2026-10-29T08:19:00Z',findings_refreshed:false}}});
assert.match(html,/Usage updated/);
assert.match(html,/Findings updated/);
assert.match(html,/full source verification repeats at completion/);
assert.doesNotMatch(html,/scientifically verified|research accepted/i);
assert.doesNotMatch(coverageTokenHtml({run:{selection_method:'coverage-guided'},profile:{roles:{}}}),/Usage updated/);
''')


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


def test_below_target_coverage_exposes_reported_scarcity_without_certifying_it(tmp_path):
    coverage = {"reviewed_papers": 27, "core_papers": 9, "supporting_papers": 18,
                "target_papers": 50, "stopping_reason": "Recorded scarcity admitted below-target run.",
                "scarcity_statement": {"reason": "No further eligible publications in the assessed pool.",
                                       "access_limits": ["Two copies remain inaccessible."],
                                       "supplemental_searches": 3}}
    run_dashboard(tmp_path, coverage, r"""
const before=JSON.stringify(graph),data={run:{selection_method:'coverage-guided'},coverage:graph};
const html=coverageSelectionHtml(data);
assert.match(html,/27 reviewed · 9 core · 18 supporting · target 50/);
assert.match(html,/Reported scarcity; not independently verified\./);
assert.match(html,/No further eligible publications in the assessed pool\./);
assert.match(html,/Two copies remain inaccessible\./);
assert.ok(html.includes('supplemental_searches&quot;:3'));
assert.doesNotMatch(html,/\[object Object\]/);
const absent=coverageSelectionHtml({...data,coverage:{...graph,scarcity_statement:null}});
assert.doesNotMatch(absent,/Reported scarcity/);
const statement='The researcher reports an exhausted eligible pool; access limits remain.';
const text=coverageSelectionHtml({...data,coverage:{...graph,scarcity_statement:statement}});
assert.ok(text.includes(statement));
assert.match(text,/Reported scarcity; not independently verified\./);
assert.equal(JSON.stringify(graph),before,'The recorded scarcity statement must remain unchanged');
""")


def test_source_scroll_survives_inspector_and_progress_refresh_without_context_leaks(tmp_path):
    graph = {"question": "Synthetic question", "claims": [], "executive_summary": [],
             "references": [{"id": "R1", "paper": {"title": "First saved paper"}},
                            {"id": "R2", "paper": {"title": "Second saved paper"}}],
             "passages": [{"id": "p1", "source_id": "R1", "kind": "fulltext",
                           "text": "Fixed recorded passage."}]}
    run_dashboard(tmp_path, graph, r"""
// A small DOM fixture: replacing innerHTML creates new elements with zero
// scroll positions. Rendering and scroll restoration remain the shipped code.
const decode=s=>s.replace(/&(amp|lt|gt|quot|#39);/g,(_,name)=>({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"})[name]);
class Element {
  constructor(tag,attrs={}) {
    this.tag=tag;this.attrs=attrs;this.children=[];this.parent=null;this.scrollTop=0;this.scrollLeft=0;
    this.hidden=Object.hasOwn(attrs,'hidden');
    this.classList={
      add:(...names)=>{this.attrs.class=[...new Set([...(this.attrs.class||'').split(/\s+/),...names])].join(' ');},
      toggle:(name,force)=>{const set=new Set((this.attrs.class||'').split(/\s+/));
        if(force===undefined ? !set.has(name) : force) set.add(name);else set.delete(name);
        this.attrs.class=[...set].join(' ');}
    };
  }
  get dataset() {return Object.fromEntries(Object.entries(this.attrs).filter(([k])=>k.startsWith('data-'))
    .map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),v]));}
  get open() {return Object.hasOwn(this.attrs,'open');}
  set open(value) {if(value)this.attrs.open='';else delete this.attrs.open;}
  get textContent() {return this.children.map(x=>x instanceof Element ? x.textContent : x).join('');}
  set innerHTML(html) {
    for(const child of this.children)if(child instanceof Element)child.parent=null;
    this.children=[];const stack=[this],voids=new Set(['br','hr','img','input','meta','link']);
    for(const token of html.match(/<[^>]+>|[^<]+/g)||[]) {
      if(token.startsWith('</')) {
        const tag=token.match(/^<\/([^\s>]+)/)?.[1];
        const at=stack.findLastIndex(e=>e.tag===tag);if(at>0)stack.length=at;
      } else if(token.startsWith('<')) {
        const match=token.match(/^<([a-z][a-z0-9-]*)([\s\S]*?)\/?\s*>$/i);if(!match)continue;
        const attrs={};for(const a of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[a[1]]=decode(a[2]||'');
        const node=new Element(match[1],attrs);node.parent=stack.at(-1);node.parent.children.push(node);
        if(!voids.has(node.tag))stack.push(node);
      } else stack.at(-1).children.push(decode(token));
    }
  }
  matches(selector) {
    if(selector.includes('>')) {const [parent,child]=selector.split('>');return this.matches(child)&&!!this.parent?.matches(parent);}
    const tag=selector.match(/^[a-z][a-z0-9-]*/i)?.[0];if(tag&&tag!==this.tag)return false;
    for(const m of selector.matchAll(/\.([\w-]+)/g))if(!(this.attrs.class||'').split(/\s+/).includes(m[1]))return false;
    for(const m of selector.matchAll(/#([\w-]+)/g))if(this.attrs.id!==m[1])return false;
    for(const m of selector.matchAll(/\[([\w-]+)\]/g))if(!Object.hasOwn(this.attrs,m[1]))return false;
    return true;
  }
  querySelectorAll(selector) {
    const result=[],selectors=selector.split(',');
    const visit=node=>{for(const child of node.children)if(child instanceof Element) {
      if(selectors.some(s=>child.matches(s.trim())))result.push(child);visit(child);
    }};visit(this);return result;
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0]||null;}
  closest(selector) {for(let node=this;node;node=node.parent)if(node.matches(selector))return node;return null;}
  contains(other) {for(let node=other;node;node=node.parent)if(node===this)return true;return false;}
  focus() {document.activeElement=this;}
  scrollIntoView() {}
}
const root=new Element('body');root.innerHTML='<div class="topbar"></div><div id="detail"></div>';
global.document={body:root,activeElement:null,getElementById:id=>root.querySelector('#'+id),
  querySelector:s=>root.querySelector(s),querySelectorAll:s=>root.querySelectorAll(s)};
global.window={matchMedia:()=>({matches:false})};
global.fetch=()=>{throw new Error('Unexpected source request: the exact contexts are already committed');};
const data=()=>({kind:'question',run:{kind:'question',selection_method:'coverage-guided',
  execution_mode:'coverage-guided full-paper research'},answer:graph,coverage:{assessed_candidates:10}});
S.runs=[{key:'q1',kind:'question',status:'researching'},{key:'q2',kind:'question',status:'researching'}];
S.research={q1:data(),q2:data()};S.sourceContexts={};S.tab='overview';
for(const question of ['q1','q2']) {
  S.selected=question;
  for(const [sid,pid,scope,label] of [['R1',null,'paper','Complete first source'],['R2',null,'paper','Complete second source'],
    ['R1',null,'section','First source section'],['R1','p1','section','Fixed passage context']]) {
    S.sourceContexts[contextKey(sid,pid,scope)]={context_label:label,paragraphs:[
      {id:'saved',text:question+' '+label+'. '+('Unchanged saved text. '.repeat(200))}
    ]};
  }
}
const paperBefore=JSON.stringify(graph),contextsBefore=JSON.stringify(S.sourceContexts);
S.selected='q1';S.inspector={kind:'paper',sourceId:'R1',scope:'paper'};
const body=()=>document.getElementById('evidence-inspector').querySelector('.inspector-body');
const position=()=>[body().scrollTop,body().scrollLeft];
const scroll=(top,left)=>{body().scrollTop=top;body().scrollLeft=left;};
renderDetail();assert.deepEqual(position(),[0,0]);
assert.match(body().textContent,/q1 Complete first source/);
scroll(420,73);const oldBody=body();paintInspector();
assert.notEqual(body(),oldBody,'The inspector must really replace its rendered DOM');
assert.deepEqual(position(),[420,73],'Direct inspector refresh preserves both axes');
scroll(710,91);const oldPane=document.getElementById('evidence-inspector');
S.research.q1.coverage.assessed_candidates=11;renderDetail();
assert.notEqual(document.getElementById('evidence-inspector'),oldPane,'Progress refresh replaces the whole detail DOM');
assert.deepEqual(position(),[710,91],'Nested inspector restoration survives full progress replacement');
assert.match(body().textContent,/q1 Complete first source/);
(async()=>{
  await inspectAnswer('paper','R2');assert.deepEqual(position(),[0,0],'A different paper starts fresh');
  assert.match(body().textContent,/q1 Complete second source/);
  scroll(111,12);await inspectAnswer('paper','R1');assert.deepEqual(position(),[0,0],'No previous source position leaks');
  scroll(155,19);await inspectAnswer('source','R1');assert.deepEqual(position(),[0,0],'Section context has its own position');
  assert.match(body().textContent,/q1 First source section/);
  scroll(180,21);await inspectAnswer('paragraphContext','p1');
  assert.deepEqual(position(),[0,0],'A different paragraph context starts fresh');
  assert.match(body().textContent,/q1 Fixed passage context/);
  scroll(280,33);S.selected='q2';renderDetail();
  assert.deepEqual(position(),[0,0],'Identical source IDs in another question cannot inherit scroll');
  assert.match(body().textContent,/q2 Fixed passage context/);
  scroll(205,17);paintInspector();assert.deepEqual(position(),[205,17]);
  S.research.q2.coverage.assessed_candidates=12;renderDetail();assert.deepEqual(position(),[205,17]);
  assert.equal(JSON.stringify(graph),paperBefore,'All raw paper passages remain unchanged');
  assert.equal(JSON.stringify(S.sourceContexts),contextsBefore,'All raw source contexts remain unchanged');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_missing_evidence_flags_only_affected_claim_and_opens_provenance_failure(tmp_path):
    graph = {"claims": [{"id": "C1", "text": "Scientist's recorded claim.", "uncertainty_score": 3,
                         "uncertainty_rationale": "Recorded scientific uncertainty.",
                         "evidence_ids": ["E_valid", "E_missing"]},
                        {"id": "C2", "text": "Unaffected recorded claim.", "uncertainty_score": 1,
                         "evidence_ids": ["E_valid"]}],
             "evidence": [{"id": "E_valid", "paragraph_id": "p1", "quote": "Exact saved source text."}],
             "passages": [{"id": "p1", "source_id": "R1", "kind": "fulltext"}],
             "references": [{"id": "R1", "paper": {"title": "Saved synthetic source"}}],
             "binding_issues": [{"kind": "missing_evidence", "claim_id": "C1", "evidence_id": "E_missing"}],
             "source_checks": [{"claim_id": "C1", "assessment": "supported",
                                "reason": "Recorded audit judgment remains distinct."}]}
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),affected=findingHtml(graph,graph.claims[0]),clean=findingHtml(graph,graph.claims[1]);
assert.match(affected,/<span role="alert"><button[^>]*data-qa-claim="C1"[^>]*>Provenance failure · details<\/button>/);
assert.match(affected,/Scientist&#39;s recorded claim\./);
assert.match(affected,/High uncertainty · 3\/3/);
assert.doesNotMatch(clean,/Provenance failure/);
const inspector=claimInspector(graph,'C1');
assert.match(inspector,/data-detail="provenance-C1"/);
assert.match(inspector,/data-qa-evidence="E_missing"/);
assert.match(inspector,/No saved evidence record matches this ID\./);
assert.match(inspector,/Audit support does not repair missing source links/);
assert.match(inspector,/Source check · Supported/);
assert.match(evidenceInspector(graph,'E_valid'),/<blockquote>Exact saved source text\.<\/blockquote>/);
S.selected='provenance';S.research.provenance={answer:graph};
global.document={getElementById:()=>null};
(async()=>{
  await inspectAnswer('claim','C1');assert.equal(S.inspector.claimId,'C1');
  await inspectAnswer('evidence','E_missing');
  assert.equal(S.inspector.kind,'evidence');assert.equal(S.inspector.evidenceId,'E_missing');
  assert.equal(S.inspector.claimId,'C1');
  const failure=evidenceInspector(graph,S.inspector.evidenceId);
  assert.match(failure,/Provenance failure: evidence not recorded/);
  assert.match(failure,/E_missing/);
  assert.match(failure,/data-qa-claim="C1"/);
  assert.doesNotMatch(failure,/<blockquote>|data-qa-context=/);
  assert.equal(JSON.stringify(graph),before,'Scientific text, uncertainty, audit and raw evidence IDs remain unchanged');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_native_availability_uses_loaded_structured_content_without_stale_markdown_or_cache(tmp_path):
    run_dashboard(tmp_path, {}, r"""
const row={key:'native',kind:'question',status:'researching',research_summary:{
  has_synthesis:true,claims_count:99,papers_count:7
}};
const empty={claims:[],executive_summary:[' ',{text:'\n'}],references:[]};
const data={run:{kind:'question',selection_method:'coverage-guided',execution_mode:'coverage-guided full-paper research'},
  answer:empty,synthesis_md:'# Synthetic question\n\n## References\nR1 · citation stub'};
S.research.native=data;
assert.equal(hasReport(row),false);assert.equal(hasFindings(row),false);
assert.equal(summaryOf(row).has_synthesis,false);assert.equal(summaryOf(row).claims_count,0);
assert.equal(hasReport({...row,status:'done'}),false,'Completed stale metadata cannot bypass the loaded graph');
assert.equal(active(row),true);assert.equal(statusInfo(row)[0],'active');
assert.equal(active({...row,status:'failed'}),false);assert.equal(statusInfo({...row,status:'failed'})[0],'attention');
assert.match(answerHtml(row,data),/Reading selected sources/);
assert.doesNotMatch(answerHtml(row,data),/aria-label="Executive summary"|<h2>Claims<\/h2>/);
const references=[{id:'R1',paper:{title:'Committed paper remains accessible'}}];
data.answer={...empty,references};
assert.equal(hasReport(row),false);assert.equal(hasFindings(row),false);
assert.equal(summaryOf(row).papers_count,1);
assert.match(modernPapersHtml(data),/Committed paper remains accessible/);
assert.match(modernPapersHtml(data),/data-qa-paper="R1"/);
data.answer={...empty,claims:[{id:'C1',text:'Actual recorded claim.',evidence_ids:[]}]};
assert.equal(hasReport(row),true);assert.equal(hasFindings(row),true);
assert.equal(summaryOf(row).claims_count,1);
assert.match(answerHtml(row,data),/Actual recorded claim/);
for(const summary of [[{text:'Actual recorded summary.',claim_ids:[]}],['Actual string summary.']]) {
  data.answer={...empty,executive_summary:summary};
  assert.equal(hasReport(row),true);assert.equal(hasFindings(row),true);
  assert.equal(qaGraph(data),data.answer);
  const html=answerHtml(row,data);assert.match(html,/aria-label="Executive summary"/);
  assert.ok(html.includes(typeof summary[0]==='string' ? summary[0] : summary[0].text));
  assert.doesNotMatch(html,/<h2>Claims<\/h2>/);
}
delete S.research.native;
assert.equal(hasReport(row),true,'Cached structured metadata is retained before graph loading');
assert.equal(hasFindings(row),true);
const legacy={key:'legacy',kind:'cycle',status:'done'};
S.research.legacy={synthesis_md:'## Findings\n\nMeaningful legacy research prose.'};
assert.equal(hasReport(legacy),true);assert.equal(hasFindings(legacy),true);
assert.equal(summaryOf(legacy).has_synthesis,true);
""")


def test_duplicate_claim_ids_open_every_record_without_applying_id_only_audit(tmp_path):
    graph = {"claims": [{"id": "C1", "claim_occurrence": 1, "text": "First distinct recorded claim.",
                         "uncertainty_score": 1, "evidence_ids": ["E1.1", "E1.2"]},
                        {"id": "C1", "claim_occurrence": 2, "text": "Second distinct recorded claim.",
                         "uncertainty_score": 3, "evidence_ids": ["E2", "E_missing"]},
                        {"id": "C2", "text": "Unique recorded claim.", "evidence_ids": ["E2"]}],
             "evidence": [{"id": "E1.1", "origin_evidence_id": "E1", "paragraph_id": "p1", "quote": "First quote."},
                          {"id": "E1.2", "origin_evidence_id": "E1", "paragraph_id": "p2", "quote": "Second quote."},
                          {"id": "E2", "paragraph_id": "p3", "quote": "Different source quote."}],
             "passages": [{"id": pid, "source_id": "R1", "kind": "fulltext"} for pid in ("p1", "p2", "p3")],
             "references": [{"id": "R1", "paper": {"title": "Saved source"}}],
             "binding_issues": [{"kind": "duplicate_claim_id", "claim_id": "C1", "record_count": 2},
                                {"kind": "missing_evidence", "claim_id": "C1", "claim_occurrence": 2,
                                 "evidence_id": "E_missing"}],
             "source_checks": [{"claim_id": "C1", "assessment": "supported"},
                               {"claim_id": "C2", "assessment": "supported"}]}
    run_dashboard(tmp_path, graph, r"""
const before=JSON.stringify(graph),html=claimInspector(graph,'C1');
assert.match(html,/C1 · Ambiguous claim ID/);
const first=html.match(/<section data-claim-occurrence="1">([\s\S]*?)<\/section>/)[1];
const second=html.match(/<section data-claim-occurrence="2">([\s\S]*?)<\/section>/)[1];
assert.match(first,/First distinct recorded claim/);assert.doesNotMatch(first,/Second distinct recorded claim/);
assert.match(first,/Low uncertainty · 1\/3/);assert.match(first,/E1 · 2 passages/);
assert.doesNotMatch(first,/E_missing|data-qa-evidence="E2"/);
assert.match(second,/Second distinct recorded claim/);assert.doesNotMatch(second,/First distinct recorded claim/);
assert.match(second,/High uncertainty · 3\/3/);assert.match(second,/data-qa-evidence="E2"/);
assert.match(second,/E_missing/);
assert.equal(qaSourceCheck(graph,'C1'),null);
assert.equal(qaSourceCheck(graph,'C2').assessment,'supported');
assert.match(html,/Source check · Not checked: ambiguous claim ID/);
assert.doesNotMatch(html,/Source check · Supported/);
for(const claim of graph.claims.slice(0,2)) assert.match(findingHtml(graph,claim),/Provenance failure · details/);
assert.doesNotMatch(findingHtml(graph,graph.claims[2]),/Provenance failure/);
const stale={...graph,binding_issues:[],claims:graph.claims.map(({claim_occurrence,...claim})=>claim)};
assert.equal(qaSourceCheck(stale,'C1'),null);
assert.match(claimInspector(stale,'C1'),/First distinct recorded claim[\s\S]*Second distinct recorded claim/);
S.selected='duplicates';S.research.duplicates={answer:graph};global.document={getElementById:()=>null};
(async()=>{
  await inspectAnswer('claim','C1');assert.equal(S.inspector.claimId,'C1');
  assert.match(claimInspector(graph,S.inspector.claimId),/Ambiguous claim ID/);
  await inspectAnswer('evidence','E2');assert.equal(S.inspector.claimId,'C1');
  await inspectAnswer('claim',S.inspector.claimId);
  assert.match(claimInspector(graph,S.inspector.claimId),/First distinct recorded claim[\s\S]*Second distinct recorded claim/);
  assert.equal(JSON.stringify(graph),before,'No duplicate claim, uncertainty, evidence or raw audit may be rewritten');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_incomplete_search_distinguishes_active_backoff_and_preserves_committed_sources(tmp_path):
    data = {"run": {"selection_method": "coverage-guided"},
            "coverage": {"discovered_candidates": 4, "search_progress": {
                "complete": False, "pending_query_ids": ["q2"],
                "queries": {"q1": {"complete": True}, "q2": {"complete": False, "deferred": True}},
                "backoff": {"provider": "openalex", "active": True,
                            "retry_not_before": "2026-10-14T14:05:00Z", "retry_after": "120",
                            "scope": "coverage_operator_shared"}}},
            "query_net": {"queries": [{"id": "q1", "search": "capture retrieval"},
                                      {"id": "q2", "search": "context effort"}]},
            "paper_selection": {"selection": {"decisions": [{"work_id": "W1", "decision": "include",
                                                             "reason": "Committed candidate decision."}]}},
            "answer": {"claims": [], "references": [{"id": "W1", "paper": {"title": "Committed accessible source"}}]}}
    run_dashboard(tmp_path, data, r"""
const before=JSON.stringify(graph),realNow=Date.now;
Date.now=()=>Date.parse("2026-10-14T14:03:00Z");
for(const html of [coverageSelectionHtml(graph),queryNetHtml(graph)]) {
  assert.match(html,/Search incomplete/);assert.match(html,/OpenAlex rate limited/);
  assert.match(html,/Retry not before <time datetime="2026-10-14T14:05:00Z">2026-10-14T14:05:00Z/);
  assert.match(html,/Pending branches: q2/);assert.match(html,/Committed candidate decision/);
  assert.doesNotMatch(html,/Search complete|disabled/);
}
const papers=modernPapersHtml(graph);
assert.match(papers,/Committed accessible source/);assert.match(papers,/data-qa-paper="W1"/);
assert.doesNotMatch(papers,/disabled/);
const pageLimited={...graph,coverage:{...graph.coverage,search_progress:{
  complete:false,pending_query_ids:['q1','q2'],queries:{q1:{complete:false},q2:{complete:false}}
}}};
for(const html of [coverageSelectionHtml(pageLimited),queryNetHtml(pageLimited)]) {
  assert.match(html,/Search incomplete/);assert.match(html,/Pending branches: q1 · q2/);
  assert.doesNotMatch(html,/rate limited|Retry not before|Search complete/);
}
const legacyProgress={...graph,coverage:{...graph.coverage,search_progress:{complete:false,
  queries:{q1:{complete:false},q2:{complete:false},q3:{complete:true}}
}}};
for(const html of [coverageSelectionHtml(legacyProgress),queryNetHtml(legacyProgress)]) {
  assert.match(html,/Search incomplete/);assert.match(html,/2 unfinished branches/);
  assert.doesNotMatch(html,/rate limited|Retry not before|Search complete/);
}
const complete={...graph,coverage:{...graph.coverage,search_progress:{complete:true,pending_query_ids:[]}}};
assert.doesNotMatch(coverageSelectionHtml(complete),/Search incomplete|rate limited|Retry not before/);
assert.doesNotMatch(queryNetHtml(complete),/Search incomplete|rate limited|Retry not before/);
Date.now=()=>Date.parse("2026-10-14T14:06:00Z");
for(const html of [coverageSelectionHtml(graph),queryNetHtml(graph)]) {
  assert.match(html,/Search incomplete/);assert.match(html,/Pending branches: q2/);
  assert.doesNotMatch(html,/OpenAlex rate limited|Retry not before/,'An expired recorded pause is not a current provider delay');
}
Date.now=realNow;
assert.equal(JSON.stringify(graph),before,'Rendering cannot change committed search progress or source records');
""")


def test_search_net_reports_successful_pages_without_asserting_unobserved_attempts(tmp_path):
    data = {"query_net": {"queries": [{"id": "q1", "search": "completed branch"}, {"id": "q2", "search": "planned branch"}]},
            "coverage": {"search_progress": {"queries": {"q1": {"pages": 2, "complete": True}, "q2": {"pages": 0, "complete": False}}}}}
    run_dashboard(tmp_path, data, r"""
const before=JSON.stringify(graph),html=queryNetHtml(graph);
assert.match(html,/q1 · 2 result pages recorded · complete/);
assert.match(html,/q2 · 0 result pages recorded · incomplete/);
const failed={...graph,coverage:{search_progress:{queries:{q1:{pages:0,complete:false,error:'HTTP429'},q2:{pages:0,complete:false,error:'HTTPError'}}}}};
const failure=queryNetHtml(failed);assert.match(failure,/q1 · 0 result pages recorded · incomplete/);assert.match(failure,/q2 · 0 result pages recorded · incomplete/);assert.doesNotMatch(failure,/not yet requested/);
const legacy=queryNetHtml({query_net:{queries:['legacy literal search']}});assert.match(legacy,/legacy literal search/);assert.doesNotMatch(legacy,/not yet requested/);
const unknown=queryNetHtml({query_net:{queries:[{id:'q1',search:'unknown receipts'}]}});assert.match(unknown,/unknown receipts/);assert.doesNotMatch(unknown,/result pages recorded|not yet requested/);
assert.equal(JSON.stringify(graph),before);
""")


def test_actual_html_table_extraction_remains_legible_in_saved_source_inspector(tmp_path):
    from canary.coverage_sources import article_text
    raw = ('<article><h2>Results</h2><p>Measured groups.</p><table><caption>Measured mass</caption>'
           '<thead><tr><th>Group</th><th>Early (mg)</th><th>Late (mg)</th></tr></thead>'
           '<tbody><tr><td>A</td><td>1.<em>23</em> mg</td><td>4.56 mg</td></tr>'
           '<tr><td>B</td><td></td><td>7.89 mg</td></tr></tbody></table></article>')
    text = article_text(raw)
    assert 'Group | Early (mg) | Late (mg)' in text
    assert 'A | 1.23 mg | 4.56 mg' in text and 'B |  | 7.89 mg' in text
    assert re.search(r'\.source-context \.context-paragraph\s*\{\s*white-space:\s*pre-wrap;', UI.read_text())
    data = {'answer': {'references': [{'id': 'R1', 'paper': {'title': 'Synthetic saved HTML source'}}]},
            'text': text}
    run_dashboard(tmp_path, data, r"""
const before=JSON.stringify(graph);S.selected='saved-table';S.sourceContexts={};
const state={sourceId:'R1',scope:'paper'};
S.sourceContexts[contextKey('R1',null,'paper')]={text_available:true,paragraphs:graph.text.split('\n\n').map(text=>({text}))};
const html=sourceInspector(graph.answer,state);
assert.match(html,/Measured mass/);assert.match(html,/Group \| Early \(mg\) \| Late \(mg\)/);
assert.match(html,/A \| 1\.23 mg \| 4\.56 mg/);assert.match(html,/B \|  \| 7\.89 mg/);
assert.ok(html.includes('class="context-paragraph '));assert.equal(JSON.stringify(graph),before);
""")


def test_compact_html_table_fallback_is_literal_accessible_source_text(tmp_path, monkeypatch):
    from canary import coverage_sources as sources
    monkeypatch.setattr(sources, "MAX_HTML_TABLE_EXPANSION", 1)
    raw = ('<article><h2>Results</h2><table><tr><td colspan="2">1.<em>23</em> mg</td>'
           '<td>4.56 mg</td></tr></table></article>')
    text = sources.article_text(raw)
    assert text == 'Results\n\n<table>\n<tr><td colspan="2">1.23 mg</td><td>4.56 mg</td></tr>\n</table>'
    data = {"answer": {"references": [{"id": "R1", "paper": {"title": "Synthetic literal source"}}]}, "text": text}
    run_dashboard(tmp_path, data, r"""
const before=JSON.stringify(graph);S.selected='literal-fallback';S.sourceContexts={};
const state={sourceId:'R1',scope:'paper'};
S.sourceContexts[contextKey('R1',null,'paper')]={text_available:true,paragraphs:graph.text.split('\n\n').map(text=>({text}))};
const html=sourceInspector(graph.answer,state);
assert.ok(html.includes(esc(graph.text.split('\n\n')[1])));
assert.match(html,/&lt;td colspan=&quot;2&quot;&gt;1\.23 mg&lt;\/td&gt;/);
assert.match(html,/4\.56 mg/);assert.doesNotMatch(html,/<table>/);
assert.equal((html.match(/1\.23 mg/g) || []).length,1);assert.equal(JSON.stringify(graph),before);
""")


def test_optional_end_tag_table_rows_remain_distinct_in_saved_source_view(tmp_path):
    from canary.coverage_sources import article_text
    text = article_text('<article><h2>Results</h2><table><tr><th>Group<th>Mass (mg)'
                        '<tr><td>A<td>1.<em>23</em><tr><td>B<td>4.56</table></article>')
    assert text == 'Results\n\nGroup | Mass (mg)\n\nA | 1.23\n\nB | 4.56'
    data = {"answer": {"references": [{"id": "R1", "paper": {"title": "Synthetic optional-tag table"}}]}, "text": text}
    run_dashboard(tmp_path, data, r"""
const before=JSON.stringify(graph);S.selected='optional-table';S.sourceContexts={};
const state={sourceId:'R1',scope:'paper'};
S.sourceContexts[contextKey('R1',null,'paper')]={text_available:true,paragraphs:graph.text.split('\n\n').map(text=>({text}))};
const html=sourceInspector(graph.answer,state);
assert.match(html,/Group \| Mass \(mg\)/);assert.match(html,/A \| 1\.23/);assert.match(html,/B \| 4\.56/);
assert.ok(html.indexOf('A | 1.23')<html.indexOf('B | 4.56'));
assert.doesNotMatch(html,/A1\.23B4\.56|A \| 1\.23B/);
assert.equal(JSON.stringify(graph),before);
""")


def test_implicit_paragraph_endings_preserve_science_sections_in_actual_source_view(tmp_path):
    from canary.coverage_sources import article_text
    from canary.coverage_runtime import canonical_passages
    raw = ('<article><p>Initial observation.<h2>Methods</h2><p>Measured 12 samples at 1.23 mg.'
           '<h2>Results</h2><p>Yield was 4.56 mg in group A.</article>')
    text = article_text(raw)
    passages = canonical_passages(text, "W1")
    expected = ["Initial observation.", "Methods", "Measured 12 samples at 1.23 mg.", "Results", "Yield was 4.56 mg in group A."]
    assert [p["text"] for p in passages] == expected
    assert all(text[p["start"]:p["end"]] == p["text"] for p in passages)
    data = {"answer": {"references": [{"id": "W1", "paper": {"title": "Synthetic saved paper"}}],
                       "passages": [dict(p, source_id="W1", kind="fulltext", anchored=True) for p in passages]},
            "text": text}
    run_dashboard(tmp_path, data, r"""
const before=JSON.stringify(graph);S.selected='paragraph-sections';S.sourceContexts={};
const selected=graph.answer.passages.at(-1),state={sourceId:'W1',paragraphId:selected.id,scope:'section'};
S.sourceContexts[contextKey('W1',selected.id,'section')]={text_available:true,offset_verified:true,
 paragraphs:graph.answer.passages.map(p=>({...p,selected:p.id===selected.id}))};
const html=sourceInspector(graph.answer,state);
assert.match(html,/<p class="context-paragraph "\s*>Methods<\/p>/);assert.match(html,/<p class="context-paragraph "\s*>Results<\/p>/);
assert.match(html,/<p class="context-paragraph "\s*>Measured 12 samples at 1\.23 mg\.<\/p>/);
assert.match(html,/<mark>Yield was 4\.56 mg in group A\.<\/mark>/);
assert.ok(html.indexOf('Methods</p>')<html.indexOf('Measured 12 samples'));
assert.ok(html.indexOf('Results</p>')<html.indexOf('Yield was 4.56 mg'));
assert.match(paragraphInspector(graph.answer,selected.id),/Yield was 4\.56 mg in group A\./);
assert.equal(JSON.stringify(graph),before);
""")


def test_xml_spanned_table_columns_reach_canonical_passages_and_actual_source_view(tmp_path):
    from canary.coverage_sources import _xml_text
    from canary.coverage_runtime import canonical_passages
    raw = (b'<article xmlns="urn:synthetic-jats"><body><sec><title>Results</title><table-wrap>'
           b'<caption><p>Measured mass.</p></caption><table><tr><th>Group</th><th>Mass</th></tr>'
           b'<tr><td rowspan="2">A</td><td>12</td></tr><tr><td>19</td></tr>'
           b'<tr><td colspan="2">Shared summary.</td></tr></table></table-wrap>'
           b'<p>Source caveat.</p></sec></body></article>')
    text = _xml_text(raw)
    passages = canonical_passages(text, "W1")
    rows = [p for p in passages if "|" in p["text"]]
    assert [[cell.strip() for cell in p["text"].split("|")] for p in rows] == [
        ["Group", "Mass"], ["A", "12"], ["", "19"], ["Shared summary.", ""]]
    assert all(text[p["start"]:p["end"]] == p["text"] for p in passages)
    data = {"answer": {"references": [{"id": "W1", "paper": {"title": "Synthetic saved JATS table"}}],
                       "passages": [dict(p, source_id="W1", kind="fulltext", anchored=True) for p in passages]},
            "selected_id": rows[2]["id"]}
    run_dashboard(tmp_path, data, r"""
const before=JSON.stringify(graph);S.selected='xml-span-layout';S.sourceContexts={};
const selected=graph.answer.passages.find(p=>p.id===graph.selected_id),state={sourceId:'W1',paragraphId:selected.id,scope:'section'};
S.sourceContexts[contextKey('W1',selected.id,'section')]={text_available:true,offset_verified:true,
 paragraphs:graph.answer.passages.map(p=>({...p,selected:p.id===selected.id}))};
const html=sourceInspector(graph.answer,state),rowText=graph.answer.passages.filter(p=>p.text.includes('|')).map(p=>p.text);
assert.deepEqual(rowText.map(text=>text.split('|').map(s=>s.trim())),[['Group','Mass'],['A','12'],['','19'],['Shared summary.','']]);
for(const text of rowText) assert.ok(html.includes(esc(text)),'Each saved logical row must remain readable');
assert.ok(html.includes('<mark>'+esc(selected.text)+'</mark>'));
assert.match(html,/Measured mass\./);assert.match(html,/Source caveat\./);
assert.doesNotMatch(html,/>19<\/p>/);assert.ok(html.indexOf(esc(rowText[1]))<html.indexOf(esc(rowText[2])));
assert.ok(paragraphInspector(graph.answer,selected.id).includes(esc(selected.text)));
assert.equal(JSON.stringify(graph),before);
""")


def test_cals_source_measurements_and_named_span_metadata_remain_inspectable(tmp_path):
    from canary.coverage_sources import _xml_text
    from canary.coverage_runtime import canonical_passages
    raw = (b'<article xmlns="urn:synthetic-cals"><body><sec><title>Results</title>'
           b'<table><title>Reported measurements.</title><tgroup cols="3">'
           b'<colspec colnum="1" colname="label"/><colspec colnum="2" colname="early"/>'
           b'<colspec colnum="3" colname="late"/><tbody><row><entry colname="label" morerows="1">A</entry>'
           b'<entry namest="early" nameend="late">1.<italic>23</italic> mg</entry></row>'
           b'<row><entry colname="late">4.<italic>56</italic> mg</entry></row></tbody></tgroup></table>'
           b'<p>Source caveat.</p></sec></body></article>')
    text = _xml_text(raw)
    assert "1.23 mg" in text and "4.56 mg" in text
    assert all(attribute in text for attribute in ('colname="late"', 'namest="early"', 'nameend="late"', 'morerows="1"'))
    passages = canonical_passages(text, "W1")
    assert all(text[p["start"]:p["end"]] == p["text"] for p in passages)
    table_passage = next(p for p in passages if "1.23 mg" in p["text"])
    data = {"answer": {"references": [{"id": "W1", "paper": {"title": "Synthetic saved CALS source"}}],
                       "passages": [dict(p, source_id="W1", kind="fulltext", anchored=True) for p in passages]},
            "selected_id": table_passage["id"]}
    run_dashboard(tmp_path, data, r"""
const before=JSON.stringify(graph);S.selected='cals-retention';S.sourceContexts={};
const selected=graph.answer.passages.find(p=>p.id===graph.selected_id),state={sourceId:'W1',paragraphId:selected.id,scope:'section'};
S.sourceContexts[contextKey('W1',selected.id,'section')]={text_available:true,offset_verified:true,
 paragraphs:graph.answer.passages.map(p=>({...p,selected:p.id===selected.id}))};
const html=sourceInspector(graph.answer,state);
assert.match(html,/1\.23 mg/);assert.match(html,/4\.56 mg/);assert.match(html,/Reported measurements\./);assert.match(html,/Source caveat\./);
for(const attribute of ['colname="late"','namest="early"','nameend="late"','morerows="1"']) assert.ok(html.includes(esc(attribute)));
assert.ok(html.includes('<mark>'+esc(selected.text)+'</mark>'));
assert.doesNotMatch(html,/<entry\b|<tgroup\b/,'Source XML is displayed as literal text, not executed as page markup');
assert.ok(paragraphInspector(graph.answer,selected.id).includes(esc(selected.text)));
assert.equal(JSON.stringify(graph),before);
""")
