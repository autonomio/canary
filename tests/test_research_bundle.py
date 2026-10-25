"""Synthetic bundle/API/CLI/consumer contracts; no provider inference or real research."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import zipfile

import httpx
import jsonschema
import pytest

from canary import bundle_connectors, cli, coverage_protocol, coverage_runtime, dashboard, research_bundle, research_spec
from canary.claim_freshness import issues as freshness_issues, unknown


DECLARATION = '''version: 1
question: Synthetic fixture question?
retrieval: {target_papers: 1}
audit: {budget_seconds: 9}
'''


def seed(workspace):
    workspace = Path(workspace)
    text='Synthetic fixture observation. This is not research evidence.'
    for folder in ('papers','texts','passages'):(workspace/folder).mkdir(exist_ok=True)
    for path in ('papers/W1.txt','texts/W1.txt'):(workspace/path).write_text(text)
    passage={'id':'W1-p0001','reference_id':'W1','page':None,'start':0,'end':len(text),'text':text}
    (workspace/'passages/W1.jsonl').write_text(json.dumps(passage)+'\n')
    sha=hashlib.sha256(text.encode()).hexdigest()
    reference={'id':'W1','title':'Synthetic source','year':2026,'authors':[], 'url':'https://example.org/synthetic',
               'file':'papers/W1.txt','text_file':'texts/W1.txt','body_sha256':sha,'text_sha256':sha}
    claim={'id':'C1','text':'A synthetic attributed finding.','kind':'reported_finding',
           'uncertainty':{'class':'moderate','score':2,'rationale':'Synthetic scope.'},'freshness':unknown(),
           'evidence_ids':['E1'],'study_result_ids':[]}
    evidence={'id':'E1','passage_ids':[passage['id']],'interpretation':'Synthetic reading.','relation':'qualifies'}
    raw={'executive_summary':[{'text':'Synthetic summary.','claim_ids':['C1']}], 'claims':[claim], 'evidence':[evidence],
        'study_results':[], 'contradictions':[],
        'gaps':[{'id':'G1','kind':'missing_data','description':'Synthetic missing comparator.','basis':'Scoped fixture.',
                 'claim_ids':['C1'],'evidence_ids':['E1'],'needed_to_resolve':'A comparator.'}],
        'residue':[{'id':'R1','observation':'A detail worth keeping.','basis':'Scoped fixture.',
                    'claim_ids':['C1'],'evidence_ids':['E1'],'why_it_matters':'Preserve it.','scope_limit':'Synthetic.'}],
        'next_questions':[], 'caveats':['Synthetic fixture only.'], 'scope_note':'Not a scientific run.'}
    payload=dict(raw,references=[reference], evidence=[dict(evidence,verbatims=[passage],reference_ids=['W1'])],
                 question_plan={'question':'Synthetic fixture question?'},independent_audit={'audit_complete':False})
    for name,value in [('investigation.json',raw),('payload.json',payload),('question-plan.json',payload['question_plan']),
                       ('reviewed-papers.json',[]),('run.json',{'kind':'question','question':'Synthetic fixture question?',
                         'execution_mode':'coverage-guided full-paper research','status':'complete','answered':True})]:
        research_bundle.write_json(workspace/name,value)
    return payload


def completed(tmp_path):
    packet=research_spec.compile_research(DECLARATION)
    research_bundle.initialize(tmp_path,packet)
    payload=seed(tmp_path)
    research_bundle.seal(tmp_path)
    return packet,payload


@pytest.mark.parametrize('source',[
    'question: a\nquestion: b', 'question: !!python/object/apply:os.system [echo forbidden]',
    'question: q\napi_key: secret', 'version: true\nquestion: q', 'question: q\nsubagents: true',
    'question: q\nresearch: {effort: low}', 'question: q\naudit: {model: other}',
    'question: q\nretrieval: {corpus: reuse}', 'question: q\nretrieval: {target_papers: false}',
    'question: q\naudit: {budget_seconds: 0}', 'question: q\nretrieval: {access: abstracts}',
    'question: q\nresearch: {command: shell}', '[]', 'question: null'])
def test_compiler_rejects_ambiguous_unsupported_or_executable_instructions(source):
    with pytest.raises(research_spec.CompileError):research_spec.compile_research(source)


def test_resolved_defaults_and_digest_are_stable_and_explicit():
    a=research_spec.compile_research('question: Q?')
    b=research_spec.compile_mapping(a['resolved'])
    assert a['spec_sha256']==b['spec_sha256']
    assert a['resolved']['research']=={'model':'gpt-6.1-sol','effort':'xhigh'}
    assert a['resolved']['audit']=={'model':'gpt-6-astra','effort':'ultra','budget_seconds':600}
    assert a['resolved']['retrieval']['target_papers']==50
    a['resolved']['audit']['budget_seconds']=1
    with pytest.raises(research_spec.CompileError):research_spec.verify_compiled(a)


def test_bundle_keeps_exact_inputs_sources_and_portable_archive(tmp_path):
    packet,payload=completed(tmp_path)
    b=research_bundle.read(tmp_path)
    assert b['specification']['source_yaml']==DECLARATION
    assert b['specification']['resolved']==packet['resolved']
    assert b['specification']['engine']['sources']['coverage_runtime.py']
    identifier=b['id']
    research_bundle.seal(tmp_path)
    assert research_bundle.read(tmp_path)['id']==identifier
    archive=io.BytesIO()
    research_bundle.archive(tmp_path,archive)
    with zipfile.ZipFile(archive) as z:
        assert z.read('research.yaml').decode()==DECLARATION
        assert z.read('papers/W1.txt')==z.read('texts/W1.txt')
        for entry in b['artifacts']:
            assert hashlib.sha256(z.read(entry['path'])).hexdigest()==entry['sha256']
    with pytest.raises(ValueError):research_bundle.initialize(tmp_path,packet)
    with pytest.raises(KeyError):research_bundle.artifact(tmp_path,'../outside')
    (tmp_path/'papers/W1.txt').write_text('Changed original')
    with pytest.raises(ValueError,match='changed'):research_bundle.artifact(tmp_path,'papers/W1.txt')


def test_modified_inputs_are_visible_and_cannot_be_silently_replayed(tmp_path):
    completed(tmp_path)
    (tmp_path/'research.yaml').write_text('question: Different?')
    with pytest.raises(ValueError,match='input changed'):research_bundle.read(tmp_path)


def test_resolvent_handoff_satisfies_pinned_consumer_contract_without_losing_finding_types(tmp_path):
    _,payload=completed(tmp_path)
    result=bundle_connectors.export(tmp_path,'resolvent')
    schema=json.loads((Path(__file__).parent/'fixtures/resolvent-submission.schema.json').read_text())
    jsonschema.Draft202012Validator(schema,format_checker=jsonschema.FormatChecker()).validate(result)
    assert result['input_method']=='source_material' and result['brief'] is None
    assert result['claims'][0]['uncertainty']=='medium'
    assert result['claims'][0]['class']=='derived_inference'
    assert result['sources'][0]['excerpt']==payload['evidence'][0]['verbatims'][0]['text']
    assert result['method_metadata']['claim_metadata']['c1']['freshness']==unknown()
    assert result['method_metadata']['residue']==payload['residue']
    assert result['method_metadata']['gaps']==payload['gaps']
    assert result['questions']==[] and result['connectors']==[]
    assert result==bundle_connectors.export(tmp_path,'resolvent')
    with pytest.raises(ValueError):bundle_connectors.export(tmp_path,'unimplemented')


def test_compiled_worker_commits_inputs_before_native_execution_and_keeps_failure(tmp_path,monkeypatch):
    staged=tmp_path/'staged.json'
    packet=research_spec.compile_research(DECLARATION)
    research_bundle.write_json(staged,packet)
    workspace=tmp_path/'new'
    def fail(directory,*args,**kwargs):
        assert (directory/'research.yaml').read_text()==DECLARATION
        assert json.loads((directory/'run.json').read_text())['minimum_papers']==1
        raise RuntimeError('Synthetic stop before inference')
    monkeypatch.setattr(coverage_runtime.shutil,'which',lambda _:sys.executable)
    monkeypatch.setattr(coverage_runtime,'scientific_session',fail)
    with pytest.raises(RuntimeError,match='Synthetic stop'):research_bundle.run_compiled(staged,workspace)
    assert research_bundle.read(workspace)['state']=='finished'
    assert json.loads((workspace/'run.json').read_text())['status']=='failed'
    assert research_bundle.read(workspace)['specification']['resolved']['audit']['budget_seconds']==9


@pytest.fixture
def api(tmp_path,monkeypatch):
    worker=tmp_path/'fixture_worker.py'
    worker.write_text('''from pathlib import Path
import os,json
from canary import research_bundle,coverage_runtime
from test_research_bundle import seed
def fake(question,workspace,**options):
    workspace=Path(workspace)
    assert not workspace.exists()
    workspace.mkdir(parents=True)
    options['on_reserved']()
    research_bundle.write_json(workspace/'actual-settings.json',{'minimum_papers':options['minimum_papers'],'audit_seconds':options['audit_seconds']})
    return seed(workspace)
coverage_runtime.run=fake
research_bundle.run_compiled(os.environ['CANARY_RUN_SPEC'],os.environ['OUT'])
''')
    real=subprocess.Popen
    calls=[]
    def launch(argv,**kwargs):
        calls.append((argv,kwargs['env']))
        kwargs['env']=dict(kwargs['env'],PYTHONPATH=str(Path(__file__).parent)+os.pathsep+str(Path(dashboard.__file__).parents[1]))
        return real([sys.executable,str(worker)],**kwargs)
    monkeypatch.setattr(dashboard.subprocess,'Popen',launch)
    server=dashboard.make_server(0,registry=tmp_path/'registry.json',root=tmp_path)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False,timeout=10) as client:
        yield server,client,calls
    server.shutdown();server.server_close();thread.join(5)


def wait_bundle(client,key):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        result=client.get(f'/api/runs/{key}/bundle').json()
        if result['bundle']['state']=='finished':return result
        time.sleep(.05)
    pytest.fail('Synthetic worker did not finish: '+str(result))


def test_api_cli_compile_execute_read_archive_and_repeat_exact_settings(api,tmp_path,capsys):
    server,client,calls=api
    declaration=tmp_path/'input.yaml';declaration.write_text(DECLARATION)
    assert cli.main(['run',str(declaration),'--dry-run','--port',str(server.server_port)])==0
    compiled=json.loads(capsys.readouterr().out)
    assert calls==[] and compiled['resolved']['audit']['budget_seconds']==9
    assert cli.main(['run',str(declaration),'--port',str(server.server_port)])==0
    started=json.loads(capsys.readouterr().out)
    result=wait_bundle(client,started['key'])
    assert result['bundle']['specification']['source_yaml']==DECLARATION
    assert result['research']['answer']['claims'][0]['freshness']['policy']=='unknown'
    assert client.get(f"/api/runs/{started['key']}/bundle/artifacts/research.yaml").text==DECLARATION
    saved=client.get(f"/api/runs/{started['key']}/bundle/archive")
    assert saved.status_code==200 and zipfile.is_zipfile(io.BytesIO(saved.content))
    exported=client.get(f"/api/runs/{started['key']}/exports/resolvent")
    assert exported.status_code==200 and exported.json()['claims'][0]['statement']=='A synthetic attributed finding.'
    repeated=client.post(f"/api/runs/{started['key']}/rerun",json={}).json()
    assert repeated['key']!=started['key']
    second=wait_bundle(client,repeated['key'])
    assert second['bundle']['id']!=result['bundle']['id']
    assert second['bundle']['specification']['resolved']==compiled['resolved']
    assert second['bundle']['specification']['spec_sha256']==started['spec_sha256']
    settings=json.loads((Path(repeated['bundle'])/'actual-settings.json').read_text())
    assert settings=={'minimum_papers':1,'audit_seconds':9}


def test_bad_request_never_spawns_and_legacy_or_corrupt_bundles_keep_their_research(api,tmp_path):
    server,client,calls=api
    assert client.post('/api/runs/compile',json={'yaml':'question: a\nquestion: b'}).status_code==400
    assert client.post('/api/runs',json={'spec':{'question':'q','shell':'bad'}}).status_code==400
    assert calls==[]
    from canary import runs
    legacy=tmp_path/'legacy';legacy.mkdir()
    seed(legacy)
    from canary.coverage_export import export_bundle
    export_bundle(legacy)
    runs.register_start('legacy','Legacy','',bundle=str(legacy),status='done',path=server.RequestHandlerClass.supervisor.registry)
    result=client.get('/api/runs/legacy/bundle').json()
    assert result['bundle']['state']=='legacy_unversioned' and result['research']['answer']['claims']
    research_bundle.initialize(legacy,research_spec.compile_research(DECLARATION))
    research_bundle.seal(legacy)
    (legacy/'research.yaml').write_text('tampered')
    result=client.get('/api/runs/legacy/bundle').json()
    assert result['bundle']['state']=='invalid' and result['research']['answer']['claims']


def test_finding_contract_keeps_residue_without_a_resolution_and_requires_claim_freshness():
    schema=coverage_protocol.investigation_schema()['properties']
    residue=schema['residue']['items']
    assert {'basis','claim_ids','evidence_ids'}<=set(residue['required'])
    assert 'follow_up_test' not in residue['properties'] and 'needed_to_resolve' not in residue['properties']
    assert 'freshness' in schema['claims']['items']['required']
    assert freshness_issues(unknown())==[]
    assert freshness_issues(dict(unknown(),policy='scheduled'))
    assert freshness_issues(dict(unknown(),policy='scheduled',review_after='2026-02-30'))
    assert freshness_issues(dict(unknown(),policy='scheduled',review_after='2026-12-01'))==[]


def test_archive_uses_one_inventory_snapshot_for_many_artifacts(tmp_path, monkeypatch):
    completed(tmp_path)
    for i in range(100):
        (tmp_path/'texts'/f'extra-{i}.txt').write_text('Synthetic artifact '+str(i))
    research_bundle.seal(tmp_path)
    read=research_bundle.read
    snapshots=[]
    def record(workspace):
        snapshots.append(workspace)
        return read(workspace)
    monkeypatch.setattr(research_bundle,'read',record)
    output=io.BytesIO()
    research_bundle.archive(tmp_path,output)
    assert len(snapshots)==1
    with zipfile.ZipFile(output) as z:
        assert z.read('texts/extra-99.txt')==b'Synthetic artifact 99'


def test_connector_rejects_wrong_verbatim_even_inside_an_intact_snapshot(tmp_path):
    completed(tmp_path)
    path=tmp_path/'payload.json'
    value=json.loads(path.read_text())
    value['evidence'][0]['verbatims'][0]['text']='Not the original passage.'
    research_bundle.write_json(path,value)
    research_bundle.seal(tmp_path)
    with pytest.raises(ValueError,match='Verbatim slice mismatch'):
        bundle_connectors.export(tmp_path,'resolvent')


def test_bundle_preserves_auxiliary_research_files_and_unicode_without_credentials(tmp_path):
    packet=research_spec.compile_research('question: How does Δ affect yield?')
    research_bundle.initialize(tmp_path,packet)
    (tmp_path/'analysis').mkdir()
    (tmp_path/'analysis/table.csv').write_bytes('condition,μ\nA,2\n'.encode())
    (tmp_path/'figure.svg').write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    (tmp_path/'auth.json').write_text('{"token":"synthetic-secret"}')
    (tmp_path/'.env').write_text('SYNTHETIC_SECRET=value')
    research_bundle.seal(tmp_path)
    output=io.BytesIO();research_bundle.archive(tmp_path,output)
    with zipfile.ZipFile(output) as z:
        assert 'Δ' in z.read('research.yaml').decode('utf-8')
        assert z.read('analysis/table.csv').decode('utf-8')=='condition,μ\nA,2\n'
        assert 'figure.svg' in z.namelist()
        assert 'auth.json' not in z.namelist() and '.env' not in z.namelist()
