"""Source-free output must retain provenance and remain usable through the API."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

import jsonschema
import pytest

from canary import bundle_connectors, bundle_encoding, compact_bundle, coverage_export, dashboard, research_bundle, research_tools, runs
from test_research_bundle import completed, api, wait_bundle
from test_coverage_evidence_ui import run_dashboard


def prepared(path):
    _, payload = completed(path)
    (path/'.run-reservation').touch()
    coverage_export.export_bundle(path)
    research_bundle.seal(path)
    return payload


def test_release_retains_exact_findings_and_standalone_consumer_contract(tmp_path):
    original = prepared(tmp_path)
    before = bundle_connectors.resolvent(tmp_path)
    manifest = compact_bundle.retain(tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {compact_bundle.NAME, compact_bundle.CHECKSUM}
    after = bundle_connectors.resolvent(tmp_path)
    for field in ('sources','claims','evidence'):
        assert after[field] == before[field]
    for field in ('gaps','residue','claim_metadata','independent_audit'):
        assert after['method_metadata'][field] == before['method_metadata'][field]
    schema = json.loads((Path(__file__).parent/'fixtures/resolvent-submission.schema.json').read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(after)
    assert compact_bundle.payload(after) == original
    assert manifest['retention']['source_bytes_retained'] is False
    assert manifest['encoding']['format'] == 'canary.references'
    assert manifest['encoding']['stored_bytes'] < manifest['encoding']['expanded_bytes']
    original_manifest = json.loads(before['result']['artifacts'][0]['content'])
    assert manifest['specification'] == original_manifest['specification']
    assert manifest['id'] == before['method_metadata']['bundle_id']
    assert [a['path'] for a in manifest['artifacts']] == ['resolvent.json']
    data = research_bundle.artifact(tmp_path, 'resolvent.json').read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest['artifacts'][0]['sha256']
    archive = io.BytesIO(); research_bundle.archive(tmp_path, archive)
    with zipfile.ZipFile(archive) as zipped:
        assert set(zipped.namelist()) == {'bundle.json','resolvent.json', compact_bundle.CHECKSUM}
    assert compact_bundle.retain(tmp_path) == manifest


def test_retained_ui_and_agent_sources_expose_exact_excerpts_without_full_paper_claim(tmp_path):
    original = prepared(tmp_path)
    compact_bundle.retain(tmp_path)
    view = dashboard.research_bundle(tmp_path)
    assert view['answer']['claims'][0]['text'] == original['claims'][0]['text']
    assert view['answer']['claims'][0]['freshness'] == original['claims'][0]['freshness']
    assert original['claims'][0]['text'] in view['synthesis_md']
    assert compact_bundle.LIMITATION in view['synthesis_md']
    assert not (tmp_path/'answer.md').exists()
    paragraph = view['answer']['passages'][0]
    context = dashboard.source_context(tmp_path, 'W1', paragraph['id'], scope='paper')
    assert context['paragraphs'][0]['text'] == original['evidence'][0]['verbatims'][0]['text']
    assert context['scope'] == 'excerpts' and context['text_complete'] is False
    assert not context['offset_verified'] and context['local_file_url'] is None
    assert context['original_url'] == 'https://example.org/synthetic'
    assert 'original bytes are not stored' in context['warning']
    row = {'bundle': str(tmp_path), 'status': 'answered'}
    sources = research_tools.execute(row, 'source', {'reference_id':'W1'}, tmp_path)
    assert sources[0]['text'] == paragraph['text'] and not sources[0]['source_bytes_retained']
    assert research_tools.execute(row, 'catalog', {}, tmp_path)[0]['body_sha256']
    with pytest.raises(ValueError):
        research_tools.execute(row, 'source', {'reference_id':'W1','limit':-1}, tmp_path)


def test_bad_evidence_cannot_release_working_materials(tmp_path):
    prepared(tmp_path)
    path = tmp_path/'payload.json'
    raw = json.loads(path.read_text()); raw['evidence'][0]['verbatims'][0]['text'] = 'Substituted words'
    research_bundle.write_json(path, raw); research_bundle.seal(tmp_path)
    with pytest.raises(ValueError, match='Verbatim slice mismatch'):
        compact_bundle.retain(tmp_path)
    assert (tmp_path/'papers/W1.txt').is_file() and not compact_bundle.exists(tmp_path)


def test_release_never_follows_working_directory_symlinks_and_detects_corruption(tmp_path):
    workspace = tmp_path/'workspace'; workspace.mkdir(); prepared(workspace)
    outside = tmp_path/'outside'; outside.mkdir(); (outside/'keep').write_text('Preserved')
    (workspace/'linked').symlink_to(outside, target_is_directory=True)
    compact_bundle.retain(workspace)
    assert (outside/'keep').read_text() == 'Preserved'
    (workspace/'resolvent.json').write_text('{}')
    with pytest.raises(ValueError, match='changed since completion'):
        research_bundle.read(workspace)


def test_old_plain_compact_bundles_reexport_smaller_without_rewriting_history(tmp_path):
    prepared(tmp_path);compact_bundle.retain(tmp_path)
    plain = compact_bundle.read(tmp_path, expanded=True)
    body = (json.dumps(plain, ensure_ascii=False, separators=(',', ':'))+'\n').encode()
    (tmp_path/compact_bundle.NAME).write_bytes(body)
    checksum = hashlib.sha256(body).hexdigest()+'\n'
    (tmp_path/compact_bundle.CHECKSUM).write_text(checksum)
    old_manifest = compact_bundle.manifest(tmp_path)
    assert 'encoding' not in old_manifest
    result = bundle_connectors.export(tmp_path, 'resolvent')
    assert bundle_encoding.header(result) is not None
    assert bundle_encoding.expand(result) == plain
    assert compact_bundle.projection(tmp_path)['answer'] == plain['method_metadata']['canary_view']['answer']
    assert (tmp_path/compact_bundle.NAME).read_bytes() == body
    assert (tmp_path/compact_bundle.CHECKSUM).read_text() == checksum


def test_manifest_sizes_include_context_added_by_a_consumer(tmp_path):
    prepared(tmp_path);compact_bundle.retain(tmp_path)
    value = compact_bundle.read(tmp_path)
    value['method_metadata']['consumer_context'] = {'note': 'Synthetic consumer addition. '*20}
    body = (bundle_encoding.dumps(value)+'\n').encode()
    (tmp_path/compact_bundle.NAME).write_bytes(body)
    (tmp_path/compact_bundle.CHECKSUM).write_text(hashlib.sha256(body).hexdigest()+'\n')
    manifest = compact_bundle.manifest(tmp_path)
    assert manifest['encoding']['stored_bytes'] == len(body)
    assert manifest['encoding']['expanded_bytes'] == len(bundle_encoding.dumps(bundle_encoding.expand(value)).encode())


def test_api_default_releases_only_the_new_request_and_keeps_operational_metrics(api, tmp_path):
    server, client, _ = api
    historical = tmp_path/'historical'; historical.mkdir(); prepared(historical)
    protected = (historical/'papers/W1.txt').read_bytes()
    created = client.post('/api/runs', json={'spec':{'question':'Synthetic fixture question?'}}).json()
    result = wait_bundle(client, created['key'])
    assert result['bundle']['retention']['mode'] == 'bundle_only'
    row = client.get('/api/runs/'+created['key']).json()
    assert row['retention']['mode'] == 'bundle_only' and row['answered']
    assert client.get('/api/log').json()['runs'][0]['answered']
    assert client.get('/api/runs/'+created['key']+'/research').json()['research']['answer']['claims']
    response = client.get('/api/runs/'+created['key']+'/exports/resolvent')
    body = response.json()
    assert bundle_encoding.header(body)['format'] == 'canary.references'
    assert response.content == bundle_encoding.dumps(body).encode('utf-8')
    expanded = client.get('/api/runs/'+created['key']+'/exports/resolvent?representation=expanded').json()
    assert expanded == bundle_encoding.expand(body)
    assert expanded['method_metadata']['canary_view']['answer']['claims']
    assert client.get('/api/runs/'+created['key']+'/exports/resolvent?representation=invalid').status_code == 400
    assert client.get('/api/runs/'+created['key']+'/sources/W1/context?scope=paper').json()['scope']=='excerpts'
    assert client.get('/api/runs/'+created['key']+'/sources/W1/file').status_code==404
    assert (historical/'papers/W1.txt').read_bytes() == protected
    supervisor = server.RequestHandlerClass.supervisor
    assert not (supervisor.root/'run-inputs'/(created['key']+'.json')).exists()


def test_compact_archive_can_be_imported_and_discovered_without_any_paper_files(tmp_path):
    from canary import footprint
    source = tmp_path/'source'; source.mkdir(); prepared(source)
    compact_bundle.retain(source)
    archive = io.BytesIO(); research_bundle.archive(source, archive)
    destination = tmp_path/'data/container-out/copied'; destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(destination)
    assert research_bundle.read(destination)['retention']['mode']=='bundle_only'
    root, registry = tmp_path/'data', tmp_path/'data/runs/registry.json'
    result = footprint.refresh(root, registry)
    assert not result['last_scan']['errors']
    assert result['registered_runs']==1
    row = runs.load(registry)[0]
    assert row['retention']['mode']=='bundle_only'
    assert row['bundle']==str(destination.resolve())
    assert dashboard.research_bundle(destination)['answer']['claims']


def test_frontend_labels_retention_and_does_not_present_excerpts_as_complete_paper(tmp_path):
    prepared(tmp_path); compact_bundle.retain(tmp_path)
    view = compact_bundle.projection(tmp_path)
    view['compiled_bundle'] = research_bundle.read(tmp_path)
    run_dashboard(tmp_path, view['answer'], """
S.selected='synthetic';
const data=""" + json.dumps(view) + """;
assert.match(bundleHtml(data),/Bundle only; full papers released/);
assert.match(bundleHtml(data),/Download research bundle/);
assert.match(bundleHtml(data),/Lossless bundle/);
assert.match(bundleHtml(data),/findings and exact excerpts unchanged/);
assert.match(bundleHtml(data),/representation=expanded/);
const state={sourceId:'W1',scope:'paper'};
S.sourceContexts={[contextKey('W1',undefined,'paper')]:{scope:'excerpts',paragraphs:[],text_available:true}};
const html=sourceInspector(graph,state);
assert.match(html,/Retained verbatim/);
assert.doesNotMatch(html,/Complete paper/);
assert.doesNotMatch(html,/Read complete paper/);
assert.match(html,/Read retained excerpts/);
""")


@pytest.mark.parametrize('action',['context','file'])
def test_inflight_source_context_crossing_release_returns_exact_retained_excerpts(api,tmp_path,monkeypatch,action):
    server,client,_=api
    workspace=tmp_path/'context-race';workspace.mkdir();original=prepared(workspace)
    registry=server.RequestHandlerClass.supervisor.registry
    runs.register_start('context-race','Fixture','',kind='question',bundle=str(workspace),status='done',path=registry)
    paragraph=dashboard.research_bundle(workspace)['answer']['passages'][0]
    resolve=dashboard.source_artifacts;released=False
    def cutover(*args,**kwargs):
        nonlocal released
        result=resolve(*args,**kwargs)
        if not released:
            released=True
            compact_bundle.retain(workspace)
        return result
    monkeypatch.setattr(dashboard,'source_artifacts',cutover)
    response=client.get('/api/runs/context-race/sources/W1/'+action,params={'scope':'paper','paragraph_id':paragraph['id']})
    if action=='file':
        assert response.status_code==404
        return
    assert response.status_code==200,response.text
    result=response.json()
    assert result['scope']=='excerpts' and result['text_complete'] is False
    assert result['local_file_url'] is None and result['offset_verified'] is False
    assert result['paragraphs'][0]['text']==original['evidence'][0]['verbatims'][0]['text']
    assert 'not stored locally' in result['warning']


def test_open_original_stream_survives_release_before_http_headers(api,tmp_path,monkeypatch):
    server,client,_=api
    workspace=tmp_path/'file-race';workspace.mkdir();prepared(workspace)
    registry=server.RequestHandlerClass.supervisor.registry
    runs.register_start('file-race','Fixture','',kind='question',bundle=str(workspace),status='done',path=registry)
    _,source,_,_=dashboard.source_artifacts(workspace,'W1')
    expected=source.read_bytes();open_file=Path.open;released=False
    def cutover(path,*args,**kwargs):
        nonlocal released
        stream=open_file(path,*args,**kwargs)
        if path==source and (args[0] if args else kwargs.get('mode'))=='rb' and not released:
            released=True
            compact_bundle.retain(workspace)
        return stream
    monkeypatch.setattr(Path,'open',cutover)
    response=client.get('/api/runs/file-race/sources/W1/file')
    assert response.status_code==200 and response.content==expected
    assert compact_bundle.exists(workspace)
    assert client.get('/api/runs/file-race/sources/W1/file').status_code==404
