"""Local adapter contracts; synthetic fixtures do not establish scientific results."""
import copy
import hashlib
import io
import json
from pathlib import Path
import threading
import zipfile

import httpx
import jsonschema
import pytest

from canary import api_contract, coverage_export, coverage_protocol, coverage_runtime as engine
from canary import dashboard, local_text, research_bundle, research_spec, research_tools, runs
from test_coverage_evidence_ui import run_dashboard


def plan(*queries):
    return {'question': 'Synthetic local question?', 'concepts': [], 'outcomes': [], 'conditions': [],
            'coverage_aspects': [], 'eligibility': {},
            'queries': [{'id': 'Q'+str(i), 'search': q} for i, q in enumerate(queries, 1)]}


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    directory = tmp_path/'library';directory.mkdir()
    body = ('Synthetic methods. A distinctive token zymofixture appears only in the body.\r\n\r\n'
            'Synthetic results. This fixture does not report actual biology.\r\n') * 6
    (directory/'paper.txt').write_bytes(body.encode())
    (directory/'empty.txt').write_text('  ')
    (directory/'broken.txt').write_bytes(b'\xff')
    (directory/'escape.txt').symlink_to(tmp_path/'outside.txt')
    (tmp_path/'outside.txt').write_text('Do not index this external document.')
    (directory/'.secret.txt').write_text('Hidden material must not be indexed.')
    (directory/'sub').mkdir();(directory/'sub'/'second.txt').write_text('Synthetic contrast null result. '*30)
    metadata=tmp_path/'metadata.jsonl'
    metadata.write_text(json.dumps({'file':'paper.txt','title':'Synthetic fixture title','doi':'10.1234/fixture',
                                  'abstract':'Synthetic metadata abstract.','year':2026,'authors':['Fixture Author']})+'\n')
    monkeypatch.setenv('CANARY_LOCAL_TEXT_ROOTS',str(tmp_path))
    workspace=tmp_path/'run';workspace.mkdir()
    config={'directory':str(directory),'metadata':str(metadata)}
    engine.write(workspace/'run.json',{'question':plan()['question'],'kind':'question','status':'researching','local_text':config})
    return workspace,config,body


def discover(workspace, *queries, pages=5):
    path=workspace/'draft.json';engine.write(path,plan(*queries))
    return engine.discover(workspace,path,pages=pages)


def test_full_text_search_scoping_metadata_hashes_and_immutable_source_chain(corpus, monkeypatch):
    workspace,config,body=corpus
    monkeypatch.setattr(engine,'_fetch_public',lambda *a:pytest.fail('Public download attempted'))
    monkeypatch.setattr(engine,'operator_key',lambda *a:pytest.fail('Public discovery attempted'))
    progress=discover(workspace,'zymofixture')
    assert progress['complete'] and progress['keyword_requests']==0 and progress['unique_candidates']==1
    manifest=engine.read(workspace/'local-corpus.json')
    assert manifest['indexed_files']==2 and len(manifest['excluded_files'])==3
    assert manifest['matched_metadata_files']==1
    candidate=json.loads((workspace/'candidates.jsonl').read_text())
    assert candidate['title']=='Synthetic fixture title' and candidate['abstract']=='Synthetic metadata abstract.'
    assert 'zymofixture' in candidate['discovery_excerpt']
    identifier=candidate['id'];assert engine.SOURCE_ID.fullmatch(identifier) and not engine.WORK_ID.fullmatch(identifier)
    receipts=engine.acquire(workspace,[identifier])
    assert receipts[0]['fresh_download'] is False
    ref=receipts[0]['reference']
    assert ref['full_text_status']=='local_text_requires_source_verification'
    assert (workspace/ref['file']).read_bytes()==body.encode()
    assert ref['body_sha256']==hashlib.sha256(body.encode()).hexdigest()
    text=(workspace/ref['text_file']).read_text()
    assert ref['text_sha256']==hashlib.sha256(text.encode()).hexdigest()
    passages=engine.source(workspace,identifier,context=0,limit=50)
    assert passages and all(text[p['start']:p['end']]==p['text'] for p in passages)
    # Export must not overwrite the supplied CRLF bytes with normalized text.
    engine.write(workspace/'run.json',dict(engine.read(workspace/'run.json'),minimum_papers=1))
    engine.write(workspace/'selection.json',{'selected_for_review_ids':[identifier]})
    engine.write(workspace/'reviewed-papers.json',[{'work_id':identifier,'eligibility':'core',
        'passage_ids_read':[passages[0]['id']],'investigated_sections':['Synthetic methods and results'],
        'substantive_review_note':'Inspected the synthetic source fixture; this is not a biological result.'}])
    from canary.claim_freshness import unknown
    engine.write(workspace/'investigation.json',{'claims':[{'id':'C1','text':'Synthetic fixture observation.',
        'evidence_ids':['E1'],'uncertainty':{'class':'high','score':3},'freshness':unknown()}],
        'evidence':[{'id':'E1','passage_ids':[passages[0]['id']],'relation':'qualifies','interpretation':'Synthetic context.'}]})
    bound=engine.bind_payload(workspace)
    assert bound['accounting']['minimum_met']
    assert bound['evidence'][0]['verbatims'][0]['reference_id']==identifier
    assert bound['mechanical_audit']['issues']==[]
    exported=coverage_export.export_bundle(workspace)
    assert (workspace/ref['file']).read_bytes()==body.encode()
    assert exported['run']['keyword_requests']==0 and 'Local UTF-8' in exported['run']['search_scope']
    paper=exported['answer']['references'][0]['paper']
    assert paper['url']=='https://doi.org/10.1234/fixture' and paper['extra']['adapter']=='local_text'
    assert 'openalex.org/L' not in json.dumps(exported)
    ctx=dashboard.source_context(workspace,identifier,passages[0]['id'],scope='paper')
    assert ctx['offset_verified'] and ctx['context_label']=='Saved source text'
    assert 'local text' in ctx['warning']


def test_changed_source_is_refused_and_acquired_bytes_survive_input_mutation(corpus):
    workspace,config,body=corpus
    discover(workspace,'zymofixture')
    identifier=engine.read(workspace/'works.json')[0]['id']
    path=Path(config['directory'])/'paper.txt'
    path.write_text('Changed outside the run.')
    receipt=engine.acquire(workspace,[identifier])[0]
    assert receipt['status']=='retrieval_or_extraction_failed' and 'changed after indexing' in receipt['reason']
    assert not engine.catalog(workspace)
    path.write_bytes(body.encode())
    assert engine.acquire(workspace,[identifier],retry_failed=True)[0]['status']=='source_retrieved'
    saved=engine.catalog(workspace)[0]
    path.unlink()
    assert (workspace/saved['file']).read_bytes()==body.encode()
    assert engine.acquire(workspace,[identifier])[0]['status']=='source_retrieved'


def test_pagination_supplemental_history_and_invalid_queries_are_explicit(corpus):
    workspace,config,_=corpus
    directory=Path(config['directory'])
    for n in range(205):(directory/f'bulk-{n:03}.txt').write_text('bulkfixture '+str(n))
    first=discover(workspace,'bulkfixture',pages=1)
    assert not first['complete'] and first['unique_candidates']==200
    assert first['queries']['Q1']['matched']==205 and first['queries']['Q1']['truncated']
    assert discover(workspace,'bulkfixture',pages=1)['unique_candidates']==205
    old=(workspace/'question-plan.json').read_bytes()
    with pytest.raises(ValueError,match='Change a query ID'):discover(workspace,'changed')
    assert (workspace/'question-plan.json').read_bytes()==old
    changed=plan('zymofixture');changed['queries'][0]['id']='Q2'
    engine.write(workspace/'draft.json',changed)
    result=engine.discover(workspace,workspace/'draft.json',pages=1)
    assert result['complete'] and set(result['queries'])=={'Q1','Q2'}
    invalid=plan('"unfinished');invalid['queries'][0]['id']='Q3'
    engine.write(workspace/'draft.json',invalid)
    with pytest.raises(ValueError,match='Invalid local text query Q3'):engine.discover(workspace,workspace/'draft.json')
    assert engine.read(workspace/'search-progress.json')==result
    assert set(engine.read(workspace/'search-progress.json')['queries'])=={'Q1','Q2'}


def test_service_allowlist_metadata_ambiguity_and_path_escape(corpus,monkeypatch):
    workspace,config,_=corpus
    monkeypatch.delenv('CANARY_LOCAL_TEXT_ROOTS')
    with pytest.raises(ValueError,match='CANARY_LOCAL_TEXT_ROOTS'):local_text.prepare(workspace,config)
    monkeypatch.setenv('CANARY_LOCAL_TEXT_ROOTS',str(Path(config['directory']).parent))
    metadata=Path(config['metadata'])
    metadata.write_text('\n'.join(json.dumps({'file':'paper.txt','doi':d,'title':t}) for d,t in [('10.1/a','A'),('10.1/b','B')]))
    manifest=local_text.prepare(workspace,config)
    assert manifest['ambiguous_metadata_files']==['paper.txt'] and manifest['matched_metadata_files']==0
    discover(workspace,'zymofixture')
    work=engine.read(workspace/'works.json')[0]
    assert work['doi'] is None and work['display_name']=='paper'
    for name in ('../outside.txt',str(metadata)):
        escaped=copy.deepcopy(work);escaped['local_text']['file']=name
        with pytest.raises(ValueError,match='inside'):local_text.fetch(escaped,config)


def test_local_configuration_compiles_without_io_and_matches_http_mcp_schema(corpus,monkeypatch):
    _,config,_=corpus
    spec={'question':'Synthetic local question?','retrieval':{'corpus':'local_text','local_text':config}}
    monkeypatch.setattr(local_text,'validate_access',lambda *a:pytest.fail('Compile accessed filesystem'))
    packet=research_spec.compile_mapping(spec)
    assert research_spec.verify_compiled(packet)==packet
    schema=api_contract.document()['components']['schemas']['ResearchDeclaration']
    jsonschema.Draft202012Validator(schema).validate(packet['resolved'])
    for retrieval in ({'corpus':'local_text'},{'local_text':config},{'corpus':'local_text','local_text':{'directory':'relative'}},
                      {'corpus':'local_text','local_text':dict(config,command='echo unsafe')}):
        with pytest.raises(research_spec.CompileError):research_spec.compile_mapping(dict(spec,retrieval=retrieval))
    prompt=coverage_protocol.research_prompt('Q?','canary tools',local_text=True)
    assert 'SQLite FTS5' in prompt and 'no network fallback' in prompt and 'Queries must use OpenAlex' not in prompt


def test_local_ui_builds_same_spec_and_exposes_scope_and_source_hashes(tmp_path):
    run_dashboard(tmp_path,{},r'''
const launch=buildLaunch({q:'Q?',mode:'question',corpus:'local_text',directory:'/fixture/texts',metadata:'/fixture/metadata.jsonl'});
assert.deepEqual(launch.spec,{question:'Q?',retrieval:{corpus:'local_text',local_text:{directory:'/fixture/texts',metadata:'/fixture/metadata.jsonl'}}});
assert.throws(()=>buildLaunch({q:'Q?',corpus:'local_text',directory:'relative'}),/absolute/);
assert.throws(()=>buildLaunch({q:'Q?',mode:'cycle',corpus:'local_text',directory:'/fixture'}),/question research flow/);
const d={run:{local_text:{}},coverage:{local_corpus:{indexed_files:12,excluded_files_count:2,matched_metadata_files:10,snapshot_sha256:'hash',search_method:'Lexical FTS5',scope_note:'Only indexed local files.'}}};
const html=localCorpusHtml(d);
assert.match(html,/12 indexed files/);assert.match(html,/2 excluded/);assert.match(html,/Snapshot SHA-256/);
const ref={id:'Lfixture',paper:{title:'Fixture',extra:{adapter:'local_text',local_file:'papers/f.source.txt',local_source:{file:'<unsafe>.txt'},body_sha256:'rawhash',text_sha256:'texthash'}}};
const source=qaCitation(ref);assert.match(source,/Saved text/);assert.match(source,/rawhash/);assert.match(source,/&lt;unsafe&gt;/);
assert.doesNotMatch(source,/Read complete paper|Saved paper/);
''')


def test_http_compile_local_tools_source_context_and_archive_use_one_adapter(corpus,monkeypatch):
    workspace,config,body=corpus
    root=workspace.parent/'service';root.mkdir()
    server=dashboard.make_server(0,root=root,registry=root/'registry.json')
    packet=research_spec.compile_mapping({'question':plan()['question'],
        'retrieval':{'corpus':'local_text','local_text':config,'target_papers':1}})
    research_bundle.initialize(workspace,packet)
    runs.register_start('fixture','Synthetic local adapter','Fixture, no inference',kind='question',
                        bundle=str(workspace),status='running',path=root/'registry.json')
    monkeypatch.setattr(server.RequestHandlerClass.supervisor,'start',lambda *a,**k: {'key':'fixture','bundle':str(workspace)})
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False,timeout=30) as client:
            compiled=client.post('/api/runs/compile',json={'spec':packet['resolved']})
            assert compiled.status_code==200 and compiled.json()['spec_sha256']==packet['spec_sha256']
            started=client.post('/api/runs',json={'spec':packet['resolved']})
            assert started.status_code==201
            outside=copy.deepcopy(packet['resolved']);outside['retrieval']['local_text']['directory']='/etc'
            refused=client.post('/api/runs',json={'spec':outside})
            assert refused.status_code==400 and 'CANARY_LOCAL_TEXT_ROOTS' in refused.json()['error']
            result=client.post('/api/runs/fixture/tools/discover',json={'plan':plan('zymofixture')})
            assert result.status_code==200 and result.json()['adapter']=='local_text',result.text
            identifier=engine.read(workspace/'works.json')[0]['id']
            result=client.post('/api/runs/fixture/tools/acquire',json={'ids':[identifier]})
            assert result.status_code==200 and result.json()['sources_available']==1,result.text
            passage=client.post('/api/runs/fixture/tools/source',json={'reference_id':identifier}).json()[0]
            engine.write(workspace/'investigation.json',{'claims':[], 'evidence':[{'id':'E1','passage_ids':[passage['id']]}]})
            engine.write(workspace/'run.json',dict(engine.read(workspace/'run.json'),status='complete'))
            runs.register_finish('fixture',status='done',path=root/'registry.json')
            coverage_export.export_bundle(workspace);research_bundle.seal(workspace)
            response=client.get(f'/api/runs/fixture/sources/{identifier}/context',params={'paragraph_id':passage['id'],'scope':'paper'})
            assert response.status_code==200 and response.json()['offset_verified'],response.text
            original=client.get(f'/api/runs/fixture/sources/{identifier}/file')
            assert original.content==body.encode()
            archive=client.get('/api/runs/fixture/bundle/archive')
            assert archive.status_code==200,archive.text[:200]
            with zipfile.ZipFile(io.BytesIO(archive.content)) as saved:
                assert not any('sqlite' in name for name in saved.namelist())
                assert 'local-corpus.json' in saved.namelist()
                assert saved.read(f'papers/{identifier}.source.txt')==body.encode()
    finally:
        server.shutdown();server.server_close();thread.join(5)


def test_runtime_indexes_before_science_and_preserves_local_mode_on_failure(corpus,monkeypatch):
    workspace,config,_=corpus
    new=workspace.parent/'new-runtime'
    def stop(directory,role,model,effort,prompt,*args,**kwargs):
        assert engine.read(directory/'local-corpus.json')['indexed_files']==2
        assert engine.read(directory/'run.json')['fresh_assets_only'] is False
        assert 'SQLite FTS5' in prompt
        raise RuntimeError('Synthetic stop before inference')
    monkeypatch.setattr(engine,'scientific_session',stop)
    with pytest.raises(RuntimeError,match='Synthetic stop'):
        engine.run(plan()['question'],new,executable='fixture-codex',local_text=config)
    assert engine.read(new/'run.json')['status']=='failed'
    assert engine.read(new/'run.json')['local_text']==config
