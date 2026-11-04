"""Native correction component acceptance on synthetic, locally saved source bytes."""
from copy import deepcopy
import json

import jsonschema
import pytest

from canary import (api_contract, audit_validation, compact_bundle, correction_runtime, corrections,
    coverage_export, coverage_runtime as engine, research_bundle, research_spec)
from test_research_bundle import seed, api
from test_audit_validation import review
from test_coverage_evidence_ui import run_dashboard


def prepared(path, rounds=3):
    research_bundle.initialize(path, research_spec.compile_mapping({'question': 'Synthetic fixture question?',
        'retrieval': {'target_papers': 1}, 'audit': {'max_correction_rounds': rounds}}))
    payload = seed(path)
    raw = json.loads((path/'investigation.json').read_text())
    raw['claims'][0]['kind'] = 'source_record'
    raw['claim_links'] = []
    for evidence in raw['evidence']: evidence['premise_claim_ids'] = []
    engine.write(path/'investigation.json', raw)
    (path/'download-receipts').mkdir()
    engine.write(path/'download-receipts/W1.json', {'reference': payload['references'][0]})
    engine.write(path/'selection.json', {'selected_for_review_ids': ['W1']})
    engine.write(path/'reviewed-papers.json', [{'work_id': 'W1', 'eligibility': 'core',
        'passage_ids_read': ['W1-p0001'], 'investigated_sections': ['synthetic methods'], 'substantive_review_note': 'Synthetic fixture only.'}])
    run = engine.read(path/'run.json');run.update(minimum_papers=1,max_correction_rounds=rounds,verification_version=1)
    engine.write(path/'run.json', run)
    payload = engine.bind_payload(path)
    assert payload['mechanical_audit']['pass']
    return payload, run


def response(payload, action='revise', split=False):
    raw = {k: deepcopy(payload[k]) for k in engine.protocol.investigation_schema()['properties']}
    for result in raw['study_results']:
        for field in result.get('fields', {}).values():
            if isinstance(field, dict): field.pop('verbatims', None)
    raw['evidence'] = [{k:e[k] for k in ('id','passage_ids','interpretation','relation','premise_claim_ids')} for e in raw['evidence']]
    record = {'claim_id': 'C1', 'action': action, 'reason': 'Synthetic source-based response.',
        'passage_ids': ['W1-p0001'], 'replacement_claim_ids': []}
    if action == 'revise':
        raw['claims'][0]['text'] = 'Revised synthetic attributed source record.'
        record['replacement_claim_ids'] = ['C1']
        if split:
            raw['claims'].append(dict(deepcopy(raw['claims'][0]), id='C2', text='Second split synthetic proposition.'))
            record['replacement_claim_ids'].append('C2')
    elif action == 'withdraw':
        raw['claims'] = []; raw['executive_summary'] = []
        for kind in ('gaps', 'residue', 'contradictions'):
            for r in raw[kind]: r['claim_ids'] = []
    return {'responses': [record], 'investigation': raw}


def audit_for(payload, assessment='supported'):
    raw = review()
    raw['claim_checks'] = [dict(deepcopy(raw['claim_checks'][0]), claim_id=c['id'], assessment=assessment,
        issue_types=['missing_scope'] if assessment=='correction_needed' else []) for c in payload['claims']]
    return raw


@pytest.mark.parametrize('action', ['revise', 'withdraw', 'dispute'])
def test_revision_withdrawal_and_dispute_are_explicit_and_typed(tmp_path, action):
    payload, _ = prepared(tmp_path)
    check = audit_validation.bind(payload, audit_for(payload, 'correction_needed'))
    proposed, withdrawn = corrections.apply(payload, check, response(payload, action))
    jsonschema.validate(response(payload, action), api_contract.schemas()['CorrectionResponse'])
    assert bool(withdrawn) == (action == 'withdraw')
    if action == 'dispute': assert proposed['claims'] == payload['claims']


@pytest.mark.parametrize('fault', ['missing_response', 'unknown_response', 'changed_unanswered', 'new_without_parent', 'invalid_passage'])
def test_invalid_corrections_cannot_silently_replace_a_claim(tmp_path, fault):
    payload, _ = prepared(tmp_path)
    check = audit_validation.bind(payload, audit_for(payload, 'correction_needed'))
    data = response(payload)
    if fault == 'missing_response': data['responses'] = []
    elif fault == 'unknown_response': data['responses'][0]['claim_id'] = 'missing'
    elif fault == 'changed_unanswered':
        payload['claims'].append(dict(deepcopy(payload['claims'][0]), id='C2'))
        data['investigation']['claims'].append(dict(deepcopy(payload['claims'][1]), text='Unexplained rewrite.'))
    elif fault == 'new_without_parent': data['investigation']['claims'].append(dict(deepcopy(payload['claims'][0]), id='C2'))
    else: data['responses'][0]['passage_ids'] = ['missing']
    with pytest.raises(ValueError): corrections.apply(payload, check, data)


def test_split_lineage_preserves_original_and_requires_each_child_to_be_rechecked(tmp_path):
    payload, _ = prepared(tmp_path)
    check = audit_validation.bind(payload, audit_for(payload, 'correction_needed'))
    proposed, _ = corrections.apply(payload, check, response(payload, split=True))
    updated = engine.bind_payload(tmp_path, investigation=proposed)
    old_final = corrections.finalize(updated, check, [])
    assert not old_final['claims'] and len(old_final['withheld_claims']) == 2
    new_check = audit_validation.bind(updated, audit_for(updated))
    final = corrections.finalize(updated, new_check, [])
    assert len(final['claims']) == 2
    assert audit_validation.snapshot(corrections.audit_basis(final)) == new_check['snapshot_sha256']


@pytest.mark.parametrize('action', ['revise', 'withdraw', 'dispute'])
def test_correction_loop_keeps_original_rounds_and_finishes_with_supported_only_claims(tmp_path, monkeypatch, action):
    payload, manifest = prepared(tmp_path, rounds=1)
    original = (tmp_path/'investigation.json').read_bytes()
    requests = []
    def scientific(workspace, role, model, effort, prompt, shape, **kwargs):
        index = kwargs['round_index']; requests.append((role,index,prompt))
        directory = workspace/'rounds'/f'{index:03d}'
        if role == 'audit':
            subject = engine.read(workspace/'audit-input.json')
            data = audit_for(subject, 'correction_needed' if index==0 or action=='dispute' else 'supported')
        else: data = response(engine.read(directory/'correction-input.json')['payload'], action)
        receipt = {'thread_id': 'writer' if role=='correction' else 'audit-'+str(index),
            'model':model,'reasoning_effort':effort,'exit_code':0,'seconds':1,'usage':{'available':False}}
        engine.write(directory/(role+'.json'), data)
        engine.write(directory/(role+'-receipt.json'), receipt)
        return data, receipt
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    result = correction_runtime.verify(tmp_path,payload,manifest,'canary tools','synthetic-codex')
    assert (tmp_path/'investigation.json').read_bytes() == original
    assert (tmp_path/'rounds/000/investigation.json').read_bytes() == original
    assert len([h for h in result['claim_revisions'] if h['kind']=='audit']) == 2
    assert result['verification']['correction_rounds'] == 1
    if action == 'revise': assert result['claims'][0]['text'].startswith('Revised') and not result['withheld_claims']
    else: assert not result['claims'] and len(result['withheld_claims']) == 1
    assert 'Synthetic source-based response.' not in requests[-1][2]  # Worker prompt explicitly forbids its inspection below.
    assert 'Do not inspect correction-input.json' in requests[-1][2]
    coverage_export.export_bundle(tmp_path)
    graph = engine.read(tmp_path/'answer.json')
    assert graph['claims'] == [] if action!='revise' else graph['source_checks'][0]['assessment']=='supported'
    run_dashboard(tmp_path, graph, """
assert.match(verificationHtml(graph),/correction rounds/);
assert.match(claimHistoryHtml(graph,'C1'),/Original finding/);
if(graph.withheld_claims.length) {
 assert.match(withheldClaimsHtml(graph),/excluded from active claims/);
 assert.equal(qaGraph({answer:graph}),graph);
}
""")


def test_zero_rounds_withholds_uncleared_claims_and_unbound_summary(tmp_path, monkeypatch):
    payload, manifest = prepared(tmp_path, rounds=0)
    monkeypatch.setattr(engine, 'scientific_session', lambda *a,**k:(audit_for(payload,'unresolved'),{}))
    result = correction_runtime.verify(tmp_path,payload,manifest,'tools','synthetic')
    assert not result['claims'] and not result['executive_summary']
    assert result['verification']['withheld_share'] == 1
    assert all(not r['claim_ids'] for r in result['gaps'])
    assert result['gaps'][0]['withheld_claim_ids'] == ['C1']


@pytest.mark.parametrize('failure', ['correction_error','changed_original','cancelled'])
def test_interruption_or_protected_input_change_cannot_leave_a_claim_active(tmp_path, monkeypatch, failure):
    payload, manifest = prepared(tmp_path, rounds=1)
    original = (tmp_path/'investigation.json').read_bytes()
    def scientific(workspace, role, *args, **kwargs):
        if role == 'audit': return audit_for(payload,'correction_needed'),{}
        if failure == 'changed_original':
            (workspace/'investigation.json').write_text('{}')
            return response(payload),{}
        if failure == 'cancelled': raise engine.RunCancelled('Synthetic cancellation')
        raise RuntimeError('Synthetic correction failure')
    monkeypatch.setattr(engine,'scientific_session',scientific)
    if failure == 'cancelled':
        with pytest.raises(engine.RunCancelled): correction_runtime.verify(tmp_path,payload,manifest,'tools','synthetic')
        result = engine.read(tmp_path/'payload.json')
    else: result = correction_runtime.verify(tmp_path,payload,manifest,'tools','synthetic')
    assert result['verification']['failure']
    failed=[r for r in result['claim_revisions'] if r.get('failure')]
    assert len(failed)==1 and failed[0]['kind']=='correction' and failed[0]['round']==1
    assert failed[0]['status']==('cancelled' if failure=='cancelled' else 'failed')
    assert not result['claims']
    assert (tmp_path/'investigation.json').read_bytes() == original


def test_round_settings_accept_existing_declarations_but_reject_noninteger_limits():
    assert research_spec.compile_research('question: q')['resolved']['audit']['max_correction_rounds'] == 3
    for value in (-1,False,1.5,None):
        with pytest.raises(research_spec.CompileError):
            research_spec.compile_mapping({'question':'q','audit':{'max_correction_rounds':value}})



def test_new_unverified_run_cannot_export_raw_claims(tmp_path):
    from canary import bundle_connectors
    prepared(tmp_path)
    coverage_export.export_bundle(tmp_path); research_bundle.seal(tmp_path)
    with pytest.raises(ValueError,match='verified final claim projection'):
        bundle_connectors.resolvent(tmp_path)


def test_final_output_history_and_withheld_claims_survive_api_export_and_retention(api,tmp_path):
    from canary import runs, bundle_connectors, bundle_encoding
    server,client,_ = api
    workspace=tmp_path/'verified-output';workspace.mkdir()
    payload,_ = prepared(workspace)
    raw=audit_for(payload,'unresolved')
    bound=audit_validation.bind(payload,raw)
    history=[{'round':0,'kind':'research','investigation':engine.read(workspace/'investigation.json'),'payload':payload},
        {'round':0,'kind':'audit','audit':bound}]
    final=corrections.finalize(payload,bound,history)
    engine.write(workspace/'payload.json',final);engine.write(workspace/'audit.json',bound)
    run=engine.read(workspace/'run.json');run.update(status='complete',answered=False)
    engine.write(workspace/'run.json',run)
    coverage_export.export_bundle(workspace);research_bundle.seal(workspace)
    (workspace/'.run-reservation').touch()
    compact_bundle.retain(workspace)
    supervisor=server.RequestHandlerClass.supervisor
    runs.register_start('verified-output','Synthetic verified output','',kind='question',bundle=str(workspace),status='done',path=supervisor.registry)
    result=client.get('/api/runs/verified-output/research')
    assert result.status_code==200,result.text
    contract=client.get('/openapi.json').json()
    schema=contract['paths']['/api/runs/{key}/research']['get']['responses']['200']['content']['application/json']['schema']
    jsonschema.validate(result.json(),dict(schema,components=contract['components']))
    graph=result.json()['research']['answer']
    assert not graph['claims'] and graph['withheld_claims'][0]['claim']['id']=='C1'
    assert graph['claim_revisions'][0]['investigation']['claims'][0]['id']=='C1'
    exported=client.get('/api/runs/verified-output/exports/resolvent').json()
    assert not exported['claims']
    assert compact_bundle.payload(bundle_encoding.expand(exported))['withheld_claims']==final['withheld_claims']
    run_dashboard(tmp_path,graph,"""
assert.equal(qaGraph({answer:graph}),graph);
assert.match(withheldClaimsHtml(graph),/Synthetic fixture|synthetic/);
assert.match(verificationHtml(graph),/100% withheld/);
""")


@pytest.mark.parametrize('edit',['added_claim','gap','source','evidence'])
def test_final_projection_never_preserves_a_verdict_after_new_shared_edits(tmp_path,edit):
    payload,_=prepared(tmp_path)
    bound=audit_validation.bind(payload,audit_for(payload))
    final=corrections.finalize(payload,bound,[])
    if edit=='added_claim': final['claims'].append(dict(deepcopy(final['claims'][0]),id='C2'))
    elif edit=='gap': final['gaps'][0]['description']+=' Changed.'
    elif edit=='source': final['references'][0]['title']+=' Changed.'
    else: final['evidence'][0]['interpretation']+=' Changed.'
    assert not audit_validation.current(corrections.audit_basis(final),final['claims'][0],bound['claim_checks'][0])


@pytest.mark.parametrize('issue', ['duplicate_review', 'minimum_unmet'])
def test_run_level_record_issues_do_not_discard_supported_claims(tmp_path, monkeypatch, issue):
    payload, manifest = prepared(tmp_path)
    if issue == 'duplicate_review':
        reviews = engine.read(tmp_path/'reviewed-papers.json')
        engine.write(tmp_path/'reviewed-papers.json', reviews * 2)
    else:
        manifest['minimum_papers'] = 2
        engine.write(tmp_path/'run.json', manifest)
    payload = engine.bind_payload(tmp_path)
    assert payload['mechanical_audit']['pass'] is False
    assert payload['mechanical_audit']['claim_blocking_issues'] == []
    requests = []
    def scientific(*args, **kwargs):
        requests.append(args[1])
        return audit_for(payload), {}
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    final = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert requests == ['audit']
    assert len(final['claims']) == 1 and not final['withheld_claims']
    assert final['verification']['run_issues'] == payload['mechanical_audit']['issues']
    coverage_export.export_bundle(tmp_path)
    graph = engine.read(tmp_path/'answer.json')
    run_dashboard(tmp_path, graph, "assert.match(verificationHtml(graph),/Run-level record check/);")


@pytest.mark.parametrize('kind', ['claims', 'evidence'])
def test_duplicate_scientific_ids_finish_with_inspectable_withheld_records(tmp_path, monkeypatch, kind):
    payload, manifest = prepared(tmp_path, rounds=0)
    raw = engine.read(tmp_path/'investigation.json')
    raw[kind].append(deepcopy(raw[kind][0]))
    engine.write(tmp_path/'investigation.json', raw)
    payload = engine.bind_payload(tmp_path)
    monkeypatch.setattr(engine, 'scientific_session', lambda *a, **k: (audit_for(payload), {}))
    final = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert not final['claims'] and final['withheld_claims']
    assert len(final['withheld_claims']) == (2 if kind == 'claims' else 1)
    if kind == 'evidence':
        assert not final['evidence'] and len(final['withheld_evidence']) == 2
    assert final['research_status'] != 'candidate_research_pending_audit'
    coverage_export.export_bundle(tmp_path)
    manifest.update(status='complete')
    engine.write(tmp_path/'run.json', manifest)
    research_bundle.seal(tmp_path)
    (tmp_path/'.run-reservation').touch()
    contribution = compact_bundle.retain(tmp_path)
    assert contribution
    assert len(compact_bundle.payload(compact_bundle.read(tmp_path))['withheld_claims']) == len(final['withheld_claims'])


@pytest.mark.parametrize('role', ['audit', 'correction'])
@pytest.mark.parametrize('name', ['scarcity.json', 'passages/injected.jsonl', 'download-receipts/injected.json'])
def test_added_scientific_files_are_rejected_and_failed_attempts_are_retained(tmp_path, monkeypatch, role, name):
    payload, manifest = prepared(tmp_path, rounds=1)
    returned = response(payload) if role == 'correction' else audit_for(payload)
    receipt = {'thread_id': 'synthetic-native', 'exit_code': 0}
    def scientific(workspace, current_role, *args, **kwargs):
        if current_role == role:
            engine.write(workspace/name, {'unexpected': 'scientific input'})
            return returned, receipt
        return audit_for(payload, 'correction_needed'), {}
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    final = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert not final['claims']
    assert 'protected' in final['verification']['failure']
    number = 0 if role == 'audit' else 1
    from canary import revision_artifacts
    artifact = revision_artifacts.read(final, number, role)
    saved = json.loads(artifact['content'])
    assert saved['raw_audit' if role == 'audit' else 'response'] == returned
    assert saved['receipt'] == receipt and saved['status'] == 'failed'
    assert engine.read(tmp_path/'run.json')['verification_failure']
    (tmp_path/'.run-reservation').touch()
    with pytest.raises(ValueError, match='working sources'):
        compact_bundle.retain(tmp_path)


def test_rejected_correction_keeps_complete_response_receipt_and_original_payload(tmp_path, monkeypatch):
    payload, manifest = prepared(tmp_path, rounds=1)
    invalid = response(payload)
    invalid['responses'] = []
    def scientific(workspace, role, *args, **kwargs):
        return (audit_for(payload, 'correction_needed'), {}) if role == 'audit' else (invalid, {'exit_code': 0})
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    final = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert not final['claims']
    from canary import revision_artifacts
    saved = json.loads(revision_artifacts.read(final, 1, 'correction')['content'])
    assert saved['response'] == invalid and saved['receipt'] == {'exit_code': 0}
    assert final['withheld_claims'][0]['claim'] == payload['claims'][0]
