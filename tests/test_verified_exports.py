"""Verified producer graph and v3 adapter behavior, with synthetic source bytes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from canary import (audit_validation, bundle_connectors, compact_bundle, corrections, coverage_export,
    coverage_runtime as engine, research_bundle, revision_artifacts, mcp_server)
from test_corrections import prepared, audit_for
from test_research_bundle import api
from test_coverage_evidence_ui import run_dashboard


def finish(workspace, *, independent=True, low=True):
    payload,_ = prepared(workspace)
    raw=engine.read(workspace/'investigation.json')
    raw['claims'][0].update(kind='reported_finding',study_result_ids=['S1'])
    if low: raw['claims'][0]['uncertainty']={'class':'low','score':1,'rationale':'Synthetic scoped judgment.'}
    raw['evidence'][0]['relation']='supports'
    raw['study_results']=[{'id':'S1','reference_ids':['W1'],'result_type':'measured',
        'fields':{name:({'text':'Synthetic value','passage_ids':['W1-p0001']} if name=='value' else None)
            for name in ('system','intervention','comparator','conditions','endpoint','value','units','sample_size','reported_uncertainty')},
        'version_or_cohort_note':'Synthetic cohort only.'}]
    engine.write(workspace/'investigation.json',raw)
    original=(workspace/'investigation.json').read_text()
    payload=engine.bind_payload(workspace)
    review=audit_for(payload)
    review['evidence_checks'][0]['assessment']='independent' if independent else 'same_study_family'
    bound=audit_validation.bind(payload,review)
    history=[{'round':0,'kind':'research','source_json':original,'investigation':raw,'payload':payload},
        {'round':0,'kind':'audit','raw_audit':review,'audit':bound}]
    final=corrections.finalize(payload,bound,history)
    engine.write(workspace/'payload.json',final);engine.write(workspace/'audit.json',bound)
    run=engine.read(workspace/'run.json');run.update(status='complete',answered=True)
    engine.write(workspace/'run.json',run)
    coverage_export.export_bundle(workspace);research_bundle.seal(workspace)
    return final,original


def test_v3_preserves_audited_class_independence_low_uncertainty_and_typed_results(tmp_path):
    final,_=finish(tmp_path)
    result=bundle_connectors.resolvent(tmp_path)
    schema=json.loads((Path(__file__).parent/'fixtures/resolvent-submission.schema.json').read_text())
    jsonschema.validate(result,schema,format_checker=jsonschema.FormatChecker())
    assert result['claims'][0]['class']=='observed_fact'
    assert result['claims'][0]['uncertainty']=='low'
    assert result['evidence'][0]['independent'] is True
    metadata=result['method_metadata']
    assert metadata['connector_version']==3
    assert metadata['study_results'][0]['claim_ids']==['c1']
    assert metadata['study_results'][0]['fields']['value']['verbatims'][0]['text'].startswith('Synthetic fixture')
    assert metadata['claim_metadata']['c1']['final_verdict']=='supported'
    assert metadata['claim_metadata']['c1']['statement_sha256']==hashlib.sha256(final['claims'][0]['text'].encode()).hexdigest()
    assert metadata['claim_revisions']==final['claim_revisions']


@pytest.mark.parametrize('kind',['recommendation','source_record','derived_comparison'])
def test_other_claim_kinds_remain_derived_candidates(tmp_path,kind):
    final,_=finish(tmp_path)
    basis=final['verification']['audit_basis'];basis['claims'][0]['kind']=kind
    payload=dict(basis,executive_summary=[],caveats=[],next_questions=[])
    bound=audit_validation.bind(payload,audit_for(payload))
    output=corrections.finalize(payload,bound,[])
    engine.write(tmp_path/'payload.json',output);engine.write(tmp_path/'audit.json',bound)
    research_bundle.seal(tmp_path)
    assert bundle_connectors.resolvent(tmp_path)['claims'][0]['class']=='derived_inference'


def test_same_study_family_and_unknown_independence_keep_the_uncertainty_floor(tmp_path):
    finish(tmp_path,independent=False)
    result=bundle_connectors.resolvent(tmp_path)
    assert result['claims'][0]['uncertainty']=='medium'
    assert not result['evidence'][0]['independent']
    assert result['method_metadata']['evidence_independence']['e1']['assessment']=='same_study_family'


def link(source,target,kind='dependency'):
    return {'id':'L1','from_claim_id':source,'to_claim_id':target,'kind':kind,'spec':'structural:moderate:outward',
        'reason':'Synthetic source-based dependency.','uncertainty':{'class':'moderate','score':2,'rationale':'Synthetic.'},'evidence_ids':['E1']}


def test_dependency_and_premise_closure_withholds_supported_dependents(tmp_path):
    payload,_=prepared(tmp_path)
    payload['claims'].extend([dict(deepcopy(payload['claims'][0]),id='C2'),dict(deepcopy(payload['claims'][0]),id='C3')])
    payload['claim_links']=[link('C1','C2')]
    payload['evidence'].append(dict(deepcopy(payload['evidence'][0]),id='E2',premise_claim_ids=['C2']))
    payload['claims'][2]['evidence_ids']=['E2']
    raw=audit_for(payload);raw['claim_checks'][0].update(assessment='unresolved')
    raw['evidence_checks'].append(dict(deepcopy(raw['evidence_checks'][0]),evidence_id='E2',assessment='unknown'))
    bound=audit_validation.bind(payload,raw)
    final=corrections.finalize(payload,bound,[])
    assert not final['claims'] and len(final['withheld_claims'])==3
    assert not final['claim_links']
    assert not any(e.get('premise_claim_ids') for e in final['evidence'])
    assert len(final['withheld_evidence'])==1
    assert 'Depends on withheld' in final['withheld_claims'][1]['reason']


def test_v3_connectors_are_only_typed_within_run_links(tmp_path):
    final,_=finish(tmp_path)
    payload=deepcopy(final['verification']['audit_basis'])
    payload.update(executive_summary=[],caveats=[],next_questions=[])
    payload['claims'].append(dict(deepcopy(payload['claims'][0]),id='C2'))
    payload['claim_links']=[link('C1','C2','coupling')]
    bound=audit_validation.bind(payload,audit_for(payload))
    final=corrections.finalize(payload,bound,[])
    engine.write(tmp_path/'payload.json',final);engine.write(tmp_path/'audit.json',bound)
    research_bundle.seal(tmp_path)
    exported=bundle_connectors.resolvent(tmp_path)
    assert exported['connectors'][0]['from']=='c1'
    assert exported['connectors'][0]['to']=='c2'
    assert exported['connectors'][0]['type']=='coupling'
    assert exported['connectors'][0]['evidence_ids']==['e1']
    schema=json.loads((Path(__file__).parent/'fixtures/resolvent-submission.schema.json').read_text())
    jsonschema.validate(exported,schema,format_checker=jsonschema.FormatChecker())


@pytest.mark.parametrize('edit',['text','added_claim','shared_record'])
def test_adapter_refuses_altered_or_unaudited_final_claims(tmp_path,edit):
    final,_=finish(tmp_path)
    if edit=='text':final['claims'][0]['text']+=' Changed.'
    elif edit=='added_claim':final['claims'].append(dict(deepcopy(final['claims'][0]),id='C2'))
    else:final['gaps'][0]['description']+=' Changed.'
    engine.write(tmp_path/'payload.json',final);research_bundle.seal(tmp_path)
    with pytest.raises(ValueError,match='current supported audit'):
        bundle_connectors.resolvent(tmp_path)


def test_exact_original_and_audit_records_survive_retention_through_api_cli_and_mcp(api,tmp_path,capsys):
    from canary import api_contract,cli,runs
    server,client,_=api
    workspace=tmp_path/'history';workspace.mkdir();final,original=finish(workspace)
    (workspace/'.run-reservation').touch();compact_bundle.retain(workspace)
    supervisor=server.RequestHandlerClass.supervisor
    runs.register_start('history','Fixture','',kind='question',bundle=str(workspace),status='done',path=supervisor.registry)
    response=client.get('/api/runs/history/revisions/0/research')
    assert response.status_code==200,response.text
    artifact=response.json()
    jsonschema.validate(artifact,api_contract.schemas()['RevisionArtifact'])
    assert artifact['content']==original
    assert hashlib.sha256(artifact['content'].encode()).hexdigest()==artifact['sha256']
    assert json.loads(client.get('/api/runs/history/revisions/0/audit').json()['content'])['raw_audit']==final['claim_revisions'][1]['raw_audit']
    assert client.get('/api/runs/history/revisions/99/audit').status_code==404
    assert cli.main(['bundle','history','--round','0','--phase','research','--port',str(server.server_port)])==0
    assert json.loads(capsys.readouterr().out)['content']==original
    assert 'canary_revision' in mcp_server.tool_catalog(client.get('/openapi.json').json())



def test_dashboard_shows_relationships_history_and_pending_verification(tmp_path):
    final,_=finish(tmp_path)
    graph=engine.read(tmp_path/'answer.json')
    graph['claim_links']=[link('C1','C1','coupling')]
    run_dashboard(tmp_path,graph,r"""
S.selected='fixture';
assert.match(historyArtifactsHtml(graph),/revisions\/0\/research/);
assert.match(historyArtifactsHtml(graph),/Original research and audit/);
assert.match(claimLinksHtml(graph),/Source-justified dependencies/);
assert.match(claimLinksHtml(graph),/data-qa-claim="C1"/);
assert.match(verificationHtml({verification_progress:{phase:'correction',round:1},history_manifest:[]}),/Findings remain provisional/);
""")


def test_revision_manifest_detects_a_changed_history_record(tmp_path):
    final,_=finish(tmp_path)
    final['claim_revisions'][1]['raw_audit']['scope_note']='Changed after recording.'
    with pytest.raises(ValueError,match='changed since its recorded digest'):
        revision_artifacts.read(final,0,'audit')



def test_coupling_with_an_uncleared_basis_drops_the_link_without_inventing_a_claim_premise(tmp_path):
    payload,_=prepared(tmp_path)
    payload['claims'].extend([dict(deepcopy(payload['claims'][0]),id='C2'),dict(deepcopy(payload['claims'][0]),id='C3')])
    payload['evidence'].append(dict(deepcopy(payload['evidence'][0]),id='E2',premise_claim_ids=['C3']))
    relationship=link('C1','C2','coupling');relationship['evidence_ids']=['E2']
    payload['claim_links']=[relationship]
    raw=audit_for(payload);raw['claim_checks'][2]['assessment']='unresolved'
    raw['evidence_checks'].append(dict(deepcopy(raw['evidence_checks'][0]),evidence_id='E2',assessment='unknown'))
    final=corrections.finalize(payload,audit_validation.bind(payload,raw),[])
    assert [c['id'] for c in final['claims']]==['C1','C2']
    assert final['claim_links']==[]


def test_original_history_cannot_disagree_with_its_retained_bytes(tmp_path):
    final,_=finish(tmp_path)
    final['claim_revisions'][0]['investigation']['claims'][0]['text']='Replaced history.'
    with pytest.raises(ValueError,match='retained JSON bytes disagree'):
        revision_artifacts.read(final,0,'research')



def test_withdrawn_only_output_keeps_inspectable_passages_and_a_report(tmp_path):
    payload,_=prepared(tmp_path)
    original_audit=audit_validation.bind(payload,audit_for(payload,'correction_needed'))
    from test_corrections import response
    data=response(payload,'withdraw')
    data['investigation']['evidence']=[]
    for kind in ('gaps','residue'):
        for record in data['investigation'][kind]:record['evidence_ids']=[]
    proposed,withdrawn=corrections.apply(payload,original_audit,data)
    candidate=engine.bind_payload(tmp_path,investigation=proposed)
    bound=audit_validation.bind(candidate,audit_for(candidate))
    final=corrections.finalize(candidate,bound,[],withdrawn)
    engine.write(tmp_path/'payload.json',final);engine.write(tmp_path/'audit.json',bound)
    coverage_export.export_bundle(tmp_path)
    graph=engine.read(tmp_path/'answer.json')
    assert not graph['claims'] and not graph['evidence']
    assert graph['passages'][0]['text']==withdrawn[0]['evidence'][0]['verbatims'][0]['text']
    report=coverage_export.render_markdown(graph)
    assert '0 supported claims; 1 withheld' in report
    assert '## Withheld claims' in report and withdrawn[0]['claim']['text'] in report
    run_dashboard(tmp_path,graph,"""
assert.match(withheldClaimsHtml(graph),/Inspect retained source passage/);
assert.match(withheldClaimsHtml(graph),/data-qa-passage="W1-p0001"/);
""")
