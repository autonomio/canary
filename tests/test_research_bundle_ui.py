from test_coverage_evidence_ui import run_dashboard


def test_bundle_inputs_artifacts_and_connector_are_visible_without_losing_residue(tmp_path):
    run_dashboard(tmp_path, {}, r'''
S.selected='fixture';
const spec={question:'Q?',research:{model:'sol',effort:'xhigh'},audit:{model:'astra',effort:'ultra',budget_seconds:600},retrieval:{target_papers:50}};
const bundle={id:'RB-fixture',state:'finished',execution:{status:'partial'},specification:{source_yaml:'question: <x>',resolved:spec,spec_sha256:'abc123',compiler_version:1,checks:['strict'],engine:{python:'3.12'}},artifacts:[{path:'papers/one file.pdf',bytes:5,sha256:'hash'}],limitations:['Not exhaustive.']};
const html=bundleHtml({compiled_bundle:bundle});
assert.match(html,/finished \/ partial/);
assert.match(html,/question: &lt;x&gt;/);
assert.match(html,/Resolved settings/);
assert.match(html,/data-action="repeat-bundle"/);
assert.match(html,/\/api\/runs\/fixture\/exports\/resolvent/);
assert.match(html,/\/bundle\/archive/);
assert.match(html,/artifacts\/papers\/one%20file.pdf/);
assert.match(bundleHtml({compiled_bundle:{state:'legacy_unversioned',specification:null,limitations:['No recorded spec.']}}),/No recorded spec/);
const g={claims:[],executive_summary:[],evidence:[],references:[],passages:[],gaps:[],contradictions:[],residue:[{id:'R1',observation:'Keep this detail.',basis:'Original context.',claim_ids:[],evidence_ids:[],why_it_matters:'Worth retaining.',scope_limit:'Scoped.'}]};
const answer=answerHtml({status:'done'},{answer:g});
assert.match(answer,/Findings/);
assert.match(answer,/Keep this detail/);
assert.doesNotMatch(answer,/No grounded answer|Follow-up:|Needed:/);
''')


def test_freshness_distinguishes_due_unknown_historical_and_triggered_review(tmp_path):
    run_dashboard(tmp_path, {}, r'''
const now=Date.parse('2026-10-25T12:00:00Z');
assert.match(freshnessHtml({},false,now),/Freshness unknown/);
assert.match(freshnessHtml({freshness:{policy:'historical',review_after:null}},false,now),/no scheduled expiry/);
assert.match(freshnessHtml({freshness:{policy:'scheduled',review_after:'2026-10-20'}},false,now),/Review due 2026-10-20/);
assert.match(freshnessHtml({freshness:{policy:'scheduled',review_after:'2026-11-20'}},false,now),/Review after 2026-11-20/);
assert.match(freshnessHtml({freshness:{policy:'scheduled',review_after:'2026-02-30'}},false,now),/unknown/);
assert.match(freshnessHtml({freshness:{policy:'event_driven',review_after:null,triggers:['New evidence']}},true,now),/New evidence/);
assert.match(freshnessHtml({freshness:{policy:'unknown',as_of:null,rationale:'<unsafe>'}},true,now),/&lt;unsafe&gt;/);
''')
