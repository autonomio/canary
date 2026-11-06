"""Native correction component acceptance on synthetic, locally saved source bytes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from canary import (api_contract, audit_validation, compact_bundle, correction_runtime, corrections,
    coverage_export, coverage_runtime as engine, mcp_server, research_bundle, research_spec)
from test_research_bundle import seed, api
from test_audit_validation import review
from test_coverage_evidence_ui import run_dashboard


def researched(path):
    """The committed outputs of a synthetic research session on saved fixture bytes."""
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


def prepared(path, rounds=3):
    research_bundle.initialize(path, research_spec.compile_mapping({'question': 'Synthetic fixture question?',
        'retrieval': {'target_papers': 1}, 'audit': {'max_correction_rounds': rounds}}))
    researched(path)
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


def two_claims(path, rounds=1):
    """Research with C1 for the audit to flag and C2 for it to pass, and that audit."""
    _, manifest = prepared(path, rounds=rounds)
    raw = engine.read(path/'investigation.json')
    raw['claims'].append(dict(deepcopy(raw['claims'][0]), id='C2', text='A second synthetic attributed source record.'))
    engine.write(path/'investigation.json', raw)
    payload = engine.bind_payload(path)
    audit = audit_for(payload)
    audit['claim_checks'][0].update(assessment='correction_needed', issue_types=['missing_scope'])
    return payload, manifest, audit


@pytest.mark.parametrize('failure', ['session_error', 'acquired_source', 'invalid_response', 'cancelled'])
def test_a_failed_correction_keeps_the_claims_the_audit_passed(tmp_path, monkeypatch, failure):
    # Issue #187: a failed correction round withheld every claim, including those the audit passed.
    payload, manifest, flagged = two_claims(tmp_path)
    def scientific(workspace, role, *args, **kwargs):
        if role == 'audit': return flagged, {}
        if failure == 'cancelled': raise engine.RunCancelled('Synthetic cancellation')
        if failure == 'session_error': raise RuntimeError('Synthetic correction failure')
        returned = response(payload)
        if failure == 'acquired_source': (workspace/'papers/W2.pdf').write_bytes(b'Synthetic acquired body')
        else: returned['responses'][0]['passage_ids'] = ['W1-r001-table5']
        return returned, {}
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    if failure == 'cancelled':
        with pytest.raises(engine.RunCancelled): correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
        final = engine.read(tmp_path/'payload.json')
    else: final = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert final['claims'] == [payload['claims'][1]] and [r['claim']['id'] for r in final['withheld_claims']] == ['C1']
    assert final['verification']['stopped'] == {'kind': 'correction', 'round': 1, 'audit_round': 0,
                                                'status': 'cancelled' if failure == 'cancelled' else 'failed'}
    assert final['withheld_claims'][0]['reason'].startswith('Verification stopped (' + final['verification']['failure'] + '). ')
    assert corrections.stopped_text(final['verification']).endswith(
        '. Correction round 1 did not finish. Only claims audit round 0 passed, unchanged since, stay active; '
        'every other claim was withheld. Working sources are kept.')


def test_a_rejected_correction_names_every_problem_with_its_ids(tmp_path):
    payload, _, flagged = two_claims(tmp_path)
    check = audit_validation.bind(payload, flagged)
    data = response(payload)
    data['responses'][0]['passage_ids'] = ['W1-r001-table5', 'W1-p0001', 'W1-r001-figure9']
    data['responses'].append(dict(data['responses'][0], claim_id='C9'))
    data['investigation']['claims'][1]['text'] += ' Unexplained rewrite.'
    with pytest.raises(ValueError) as rejected: corrections.apply(payload, check, data)
    assert str(rejected.value) == ('Correction response rejected: responses name claims that do not exist: C9; '
        'C1 cites passage IDs that are not source passages of this run: W1-r001-table5, W1-r001-figure9; '
        'changed or removed claims without a response: C2')
    assert corrections.problem(payload, check, data) == str(rejected.value)
    assert corrections.problem(payload, check, {'responses': []}) == (
        "Correction response does not match its schema at the top level: 'investigation' is a required property")
    assert corrections.problem(payload, check, response(payload)) is None


def test_a_sessions_working_files_are_removed_and_do_not_fail_the_run(tmp_path, monkeypatch):
    payload, manifest = prepared(tmp_path, rounds=1)
    def scientific(workspace, role, *args, **kwargs):
        directory = workspace/'rounds'/f"{kwargs['round_index']:03d}"
        (directory/'source-audit').mkdir(exist_ok=True)
        (directory/'audit_read.py').write_text('print(1)')
        (directory/'source-audit'/'W1.json').write_text('{}')
        return audit_for(payload), {}
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    result = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert result['verification']['failure'] is None and result['claims']
    assert not (tmp_path/'rounds/000/audit_read.py').exists()
    assert not (tmp_path/'rounds/000/source-audit/W1.json').exists()
    assert json.loads((tmp_path/'rounds/000/audit-working-files.json').read_text()) == {'removed_files': 2, 'removed_bytes': 10}


def test_a_planted_link_in_a_round_cannot_write_outside_the_workspace(tmp_path, monkeypatch):
    workspace = tmp_path/'workspace'; workspace.mkdir()
    outside = tmp_path/'outside.txt'; outside.write_text('untouched')
    payload, manifest = prepared(workspace, rounds=1)
    def scientific(root, role, *args, **kwargs):
        (root/'rounds'/f"{kwargs['round_index']:03d}"/'audit-working-files.json').symlink_to(outside)
        return audit_for(payload), {}
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    result = correction_runtime.verify(workspace, payload, manifest, 'tools', 'synthetic')
    assert outside.read_text() == 'untouched'
    assert not (workspace/'rounds/000/audit-working-files.json').is_symlink()
    assert result['verification']['failure'] is None


def test_a_source_first_cited_by_the_audit_is_exported_without_failing_the_correction(tmp_path, monkeypatch):
    payload, manifest = prepared(tmp_path, rounds=1)
    text, body = 'A second synthetic source that no claim cites.', b'Synthetic saved body'
    (tmp_path/'texts/W2.txt').write_text(text)
    (tmp_path/'papers/W2.pdf').write_bytes(body)
    passage = {'id': 'W2-p0001', 'reference_id': 'W2', 'page': None, 'start': 0, 'end': len(text), 'text': text}
    (tmp_path/'passages/W2.jsonl').write_text(json.dumps(passage)+'\n')
    engine.write(tmp_path/'download-receipts/W2.json', {'reference': dict(payload['references'][0], id='W2',
        file='papers/W2.pdf', text_file='texts/W2.txt', body_sha256=hashlib.sha256(body).hexdigest(),
        text_sha256=hashlib.sha256(text.encode()).hexdigest())})
    payload = engine.bind_payload(tmp_path)
    finding = {'id': 'F1', 'severity': 'material', 'description': 'A source no claim cites qualifies the finding.',
        'claim_ids': ['C1'], 'reference_ids': ['W2'], 'passage_ids': ['W2-p0001']}
    def scientific(workspace, role, *args, **kwargs):
        # The refresh at the start of a session and the one at its end, as coverage_runtime runs them.
        coverage_export.export_bundle(workspace)
        coverage_export.export_bundle(workspace)
        index = kwargs['round_index']
        if role == 'correction':
            return response(engine.read(workspace/f'rounds/{index:03d}/correction-input.json')['payload']), {}
        subject = engine.read(workspace/'audit-input.json')
        return (audit_for(subject) if index else dict(audit_for(subject, 'correction_needed'), findings=[finding])), {}
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    result = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert result['verification']['failure'] is None and result['claims']
    assert (tmp_path/'papers/W2.txt').read_text() == text
    assert not list((tmp_path/'papers').glob('*.export.tmp'))


def test_a_temporary_link_left_by_an_earlier_export_is_not_a_new_input(tmp_path, monkeypatch):
    payload, manifest = prepared(tmp_path, rounds=1)
    def scientific(workspace, role, *args, **kwargs):
        (workspace/'papers/W1.txt.export.tmp').write_bytes((workspace/'texts/W1.txt').read_bytes())
        return audit_for(payload), {}
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    assert correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')['verification']['failure'] is None


@pytest.mark.parametrize('planted', ['rounds/001/note.json', 'passages/planted.jsonl', 'papers/planted.pdf',
    'papers/W1.txt', 'papers/W1.txt.export.tmp'])
def test_a_new_file_outside_the_sessions_own_round_still_fails_the_run(tmp_path, monkeypatch, planted):
    payload, manifest = prepared(tmp_path, rounds=1)
    def scientific(workspace, role, *args, **kwargs):
        (workspace/planted).parent.mkdir(parents=True, exist_ok=True)
        (workspace/planted).write_text('{}')
        return audit_for(payload), {}
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    result = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert result['verification']['failure'] and not result['claims']


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


class ScriptedAudit:
    """A native audit session whose persisted files and exit code are scripted."""
    backend = 'codex_exec'

    def __init__(self, output, exit_code, writes):
        self.output, self.code, self.writes = output, exit_code, writes
        self.home, self.thread_id, self.exit_code, self.stderr = str(output.parent), 'scripted-audit', None, None

    def invocation(self):
        return {'backend': self.backend}

    def start(self, prompt):
        # Supported verdicts for every claim; none may become active after a dead session.
        verdicts = audit_for(engine.read(self.output.parents[2]/'audit-input.json'))
        if 'partial' in self.writes: engine.write(self.output.parent/'audit.json', dict(verdicts, audit_complete=False))
        if 'final' in self.writes: engine.write(self.output, verdicts)

    def __iter__(self):
        return iter([json.dumps({'type': 'thread.started', 'thread_id': self.thread_id})])

    def wait(self):
        self.exit_code = self.code
        return self.code

    def interrupt(self): pass
    def archive(self): return {'archived': True}
    def close(self): pass


class ScriptedExecution:
    def __init__(self, exit_code, writes):
        self.exit_code, self.writes = exit_code, writes

    def context(self):
        return {'CANARY_CODEX_EXECUTABLE': 'scripted-codex', 'CANARY_CODEX_CREDENTIALS_STORE': 'file'}

    def open(self, key, role, workspace, model, effort, shape, output, *args, **options):
        return ScriptedAudit(output, self.exit_code, self.writes)


def run_research(tmp_path, monkeypatch, audit):
    """The real run(): synthetic committed research, then the given audit session."""
    monkeypatch.delenv('RUN_KEY', raising=False)
    monkeypatch.delenv('CANARY_CANCEL_MARKER', raising=False)
    def scientific(workspace, role, model, effort, prompt, shape, **kwargs):
        if role != 'research':
            return audit(workspace, role, model, effort, prompt, shape, **kwargs)
        manifest = engine.read(workspace/'run.json')
        researched(workspace)
        engine.write(workspace/'run.json', manifest)
        return engine.read(workspace/'investigation.json'), {'exit_code': 0}
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    workspace = tmp_path/'run'
    return workspace, engine.run('Synthetic fixture question?', workspace, minimum_papers=1,
                                 executable='scripted-codex', max_correction_rounds=1)


@pytest.mark.parametrize('exit_code,writes', [(1, ('partial',)), (1, ('final',)), (0, ('partial',)), (1, ())],
    ids=['partial_nonzero_exit', 'final_nonzero_exit', 'partial_zero_exit_no_final', 'nothing_persisted'])
def test_dead_audit_session_fails_verification_and_keeps_working_sources(tmp_path, monkeypatch, exit_code, writes):
    from canary import revision_artifacts
    def audit(workspace, role, model, effort, prompt, shape, **kwargs):
        return engine._scientific_session(workspace, role, model, effort, prompt, shape, executable='scripted-codex',
            audit_timeout=None, execution=ScriptedExecution(exit_code, writes), run_key='scripted', environment={},
            round_index=kwargs['round_index'], resume_thread_id=None, expected_backend=None, resume_archived=True)
    workspace, final = run_research(tmp_path, monkeypatch, audit)
    run = engine.read(workspace/'run.json')
    assert not final['claims'] and [r['status'] for r in final['withheld_claims']] == ['withheld']
    assert final['verification']['failure'].startswith('RuntimeError: Audit session ')
    assert run['status'] == 'partial' and run['verification_failure'] == final['verification']['failure']
    assert [h['kind'] for h in final['claim_revisions']] == ['research', 'audit']
    failed = json.loads(revision_artifacts.read(final, 0, 'audit')['content'])
    assert failed['status'] == 'failed' and failed['receipt']['exit_code'] == exit_code
    saved = engine.read(workspace/'rounds/000'/('audit-response.json' if 'final' in writes else 'audit.json'))
    assert json.loads(failed['response_text']) == saved
    if writes == ('partial',): assert saved['audit_complete'] is False and saved['limitations']
    (workspace/'.run-reservation').touch()
    with pytest.raises(ValueError, match='working sources'):
        compact_bundle.retain(workspace)
    assert (workspace/'papers/W1.txt').is_file()
    coverage_export.export_bundle(workspace)
    graph = engine.read(workspace/'answer.json')
    assert graph['audit']['status'] == 'failed' and engine.read(workspace/'run.json')['audit_status'] == 'failed'
    run_dashboard(tmp_path, graph, """
assert.match(verificationHtml(graph),/Verification stopped: RuntimeError: Audit session /);
assert.match(coverageAuditHtml({run:{selection_method:'coverage-guided'},answer:graph}),/Independent audit · failed/);
""")


def run_level(claim_ids):
    return {'id': 'F1', 'severity': 'material', 'description': 'Synthetic selection omits a comparator family.',
            'claim_ids': claim_ids, 'reference_ids': ['W1'], 'passage_ids': ['W1-p0001']}


@pytest.mark.parametrize('claim_ids', [[], ['GONE']], ids=['unscoped', 'unknown_only'])
def test_run_level_material_finding_keeps_supported_claims_and_marks_the_run_partial(tmp_path, monkeypatch, claim_ids):
    roles = []
    def audit(workspace, role, *args, **kwargs):
        roles.append(role)
        return dict(audit_for(engine.read(workspace/'audit-input.json')), findings=[run_level(claim_ids)]), {'exit_code': 0}
    workspace, final = run_research(tmp_path, monkeypatch, audit)
    assert roles == ['audit']  # One audit and no correction round.
    assert [c['id'] for c in final['claims']] == ['C1'] and not final['withheld_claims']
    assert final['verification']['run_findings'] == [run_level(claim_ids)] and final['verification']['failure'] is None
    assert engine.read(workspace/'run.json')['status'] == 'partial'
    unknown = ['Audit finding F1 names unknown claim IDs: GONE'] if claim_ids else []
    assert final['independent_audit']['validation_issues'] == unknown


@pytest.mark.parametrize('claim_ids', [[], ['GONE']], ids=['unscoped', 'unknown_only'])
def test_run_level_finding_reaches_export_api_cli_mcp_and_dashboard(api, tmp_path, monkeypatch, capsys, claim_ids):
    from canary import bundle_connectors, cli, runs
    server, client, _ = api
    workspace = tmp_path/'run-finding'; workspace.mkdir()
    payload, manifest = prepared(workspace)
    monkeypatch.setattr(engine, 'scientific_session', lambda *a, **k: (dict(audit_for(payload), findings=[run_level(claim_ids)]), {}))
    final = correction_runtime.verify(workspace, payload, manifest, 'tools', 'synthetic')
    manifest.update(status='partial', answered=True)
    engine.write(workspace/'run.json', manifest)
    coverage_export.export_bundle(workspace); research_bundle.seal(workspace)
    exported = bundle_connectors.resolvent(workspace)
    schema = json.loads((Path(__file__).parent/'fixtures/resolvent-submission.schema.json').read_text())
    jsonschema.validate(exported, schema, format_checker=jsonschema.FormatChecker())
    assert [c['statement'] for c in exported['claims']] == [final['claims'][0]['text']]
    assert any(line.startswith('Run-level audit finding') and run_level(claim_ids)['description'] in line
               for line in exported['result']['limitations'])
    runs.register_start('run-finding', 'Synthetic run-level finding', '', kind='question', bundle=str(workspace),
                        status='done', path=server.RequestHandlerClass.supervisor.registry)
    research = client.get('/api/runs/run-finding/research')
    assert research.status_code == 200, research.text
    contract = client.get('/openapi.json').json()
    tool = mcp_server.tool_catalog(contract)['canary_research']['tool']
    jsonschema.validate(research.json(), tool.outputSchema)
    graph = research.json()['research']['answer']
    assert graph['verification']['run_findings'] == [run_level(claim_ids)]
    assert cli.main(['bundle', 'run-finding', '--port', str(server.server_port)]) == 0
    assert json.loads(capsys.readouterr().out)['research']['answer']['verification']['run_findings'] == [run_level(claim_ids)]
    run_dashboard(tmp_path, graph, r"""
const html=verificationHtml(graph);
assert.match(html,/Run-level audit finding · material: Synthetic selection omits a comparator family/);
assert.match(html,/No claim was withheld for it/);
assert.equal(/names no current claim \(GONE\)/.test(html), graph.verification.run_findings[0].claim_ids.length>0);
""")


@pytest.mark.parametrize('stop', ['dead_audit', 'cancelled'])
def test_audit_stopped_after_a_correction_reports_no_corrected_claim_finding_as_run_level(tmp_path, monkeypatch, stop):
    payload, manifest = prepared(tmp_path, rounds=1)
    finding = dict(run_level(['C1']), description='Synthetic defect in C1.')
    def scientific(workspace, role, *args, **kwargs):
        if role == 'correction':
            return response(engine.read(workspace/'rounds/001/correction-input.json')['payload'], 'withdraw'), {'exit_code': 0}
        if kwargs['round_index'] == 0:
            return dict(audit_for(payload, 'correction_needed'), findings=[finding]), {}
        if stop == 'cancelled':
            raise engine.RunCancelled('Synthetic cancellation')
        raise RuntimeError('Audit session exited with code 1; its intermediate audit is preserved and no verdict was accepted')
    monkeypatch.setattr(engine, 'scientific_session', scientific)
    if stop == 'cancelled':
        with pytest.raises(engine.RunCancelled):
            correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
        final = engine.read(tmp_path/'payload.json')
    else:
        final = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert final['verification']['failure'] and not final['claims']
    assert [r['status'] for r in final['withheld_claims']] == ['withdrawn']
    # The last bound audit predates the withdrawal; its finding on C1 is not a run-level finding.
    assert final['independent_audit']['findings'] == [finding] and final['verification']['run_findings'] == []


def test_finding_naming_an_unknown_claim_flags_only_the_current_claims_it_names(tmp_path):
    payload, _ = prepared(tmp_path)
    payload['claims'].append(dict(deepcopy(payload['claims'][0]), id='C2'))
    raw = dict(audit_for(payload), findings=[dict(run_level(['C1', 'GONE']), description='Synthetic scope defect in C1.')])
    bound = audit_validation.bind(payload, raw)
    assert corrections.targets(payload, bound) == ['C1']
    assert bound['validation_issues'] == ['Audit finding F1 names unknown claim IDs: GONE'] and not bound['audit_complete']
    final = corrections.finalize(payload, bound, [])
    assert [c['id'] for c in final['claims']] == ['C2'] and not final['verification']['run_findings']
    assert final['withheld_claims'][0]['reason'] == 'Material finding remains: Synthetic scope defect in C1.'


@pytest.mark.parametrize('defect', ['missing_study_result', 'uncertainty_mismatch'])
def test_one_claim_record_failure_withholds_only_that_claim(tmp_path, monkeypatch, defect):
    payload, manifest = prepared(tmp_path, rounds=0)
    raw = engine.read(tmp_path/'investigation.json')
    raw['claims'].extend(dict(deepcopy(raw['claims'][0]), id=identifier) for identifier in ('C2', 'C3'))
    if defect == 'missing_study_result':
        raw['claims'][1]['kind'] = 'reported_finding'
    else:
        raw['claims'][1]['uncertainty'] = dict(raw['claims'][1]['uncertainty'], score=3)
    engine.write(tmp_path/'investigation.json', raw)
    payload = engine.bind_payload(tmp_path)
    mechanical = payload['mechanical_audit']
    assert not mechanical['pass'] and mechanical['claim_blocking_issues'] == [] and list(mechanical['claim_issues']) == ['C2']
    monkeypatch.setattr(engine, 'scientific_session', lambda *a, **k: (audit_for(payload), {}))
    final = correction_runtime.verify(tmp_path, payload, manifest, 'tools', 'synthetic')
    assert corrections.targets(payload, final['independent_audit']) == ['C2']
    assert [c['id'] for c in final['claims']] == ['C1', 'C3']
    assert [(r['claim']['id'], r['reason']) for r in final['withheld_claims']] == [
        ('C2', 'Claim record checks failed: ' + '; '.join(mechanical['claim_issues']['C2']))]
    assert final['verification']['run_issues'] == [] and not final['verification']['failure']
