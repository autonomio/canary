"""Synthetic provider contracts; live research has separate operational receipts."""
import hashlib
import json
import threading
from pathlib import Path

import httpx
import jsonschema
import pytest

from canary import api_contract, bundle_connectors, cli, coverage_export, dashboard, research_bundle, research_spec
from canary import coverage_runtime as engine, source_adapters as adapters
from canary.candidate_index import CandidateIndex
from canary.claim_freshness import unknown
from test_coverage_evidence_ui import run_dashboard


TRIAL = {'protocolSection': {
    'identificationModule': {'nctId': 'NCT00000001', 'briefTitle': 'Synthetic clinical registry fixture'},
    'descriptionModule': {'briefSummary': 'Synthetic protocol description. ' * 30},
    'statusModule': {'overallStatus': 'COMPLETED', 'lastUpdatePostDateStruct': {'date': '2026-01-02'}},
    'referencesModule': {'references': [{'pmid': '1234', 'type': 'RESULT'}]}},
    'hasResults': True, 'resultsSection': {'outcomeMeasuresModule': {'outcomeMeasures': [
        {'title': 'Synthetic outcome', 'timeFrame': '12 weeks', 'groups': [{'id': 'G1', 'title': 'Synthetic arm'}],
         'denoms': [{'units': 'participants', 'counts': [{'groupId': 'G1', 'value': '12'}]}],
         'classes': [{'categories': [{'measurements': [{'groupId': 'G1', 'value': '4.2', 'spread': '0.6'}]}]}]}]}}}
OSF = {'@id': 'https://osf.io/abc12', 'resourceType': [{'@id': 'Registration'}],
       'title': [{'@value': 'Synthetic registered plan'}], 'description': [{'@value': 'Planned synthetic analysis.'}],
       'dateModified': [{'@value': '2026-01-03'}]}
REGISTRATION = {'data': {'id': 'abc12', 'attributes': {'public': True, 'title': 'Synthetic registered plan',
    'description': 'Synthetic study plan, not findings. ' * 25, 'date_registered': '2026-01-01',
    'date_modified': '2026-01-03', 'withdrawn': False, 'registration_supplement': 'Synthetic fixture form',
    'registered_meta': {'q1': {'value': 'We plan this analysis.'}}},
    'relationships': {'registration_schema': {'data': {'id': 'a' * 24}}}}}
PATENT = b'''<world-patent-data><biblio-search total-result-count="1"><exchange-documents>
<exchange-document country="EP" doc-number="1234567" kind="A1" family-id="999">
<bibliographic-data><publication-reference><document-id><date>20260104</date></document-id></publication-reference>
<invention-title lang="en">Synthetic patent disclosure</invention-title></bibliographic-data>
<abstract lang="en"><p>Synthetic disclosure abstract.</p></abstract></exchange-document>
</exchange-documents></biblio-search></world-patent-data>'''


def plan(*providers):
    return {'question': 'Synthetic optional source question?', 'concepts': [], 'outcomes': [], 'conditions': [],
            'coverage_aspects': [], 'eligibility': {},
            'queries': [{'id': 'q' + str(i), 'source': p, 'search': 'synthetic' if p != 'epo' else 'ta=synthetic'}
                        for i, p in enumerate(('openalex', *providers))]}


def mock_provider(request):
    host, path = request.url.host, request.url.path
    if host == 'api.openalex.org':
        return httpx.Response(200, json={'results': [], 'meta': {'count': 0, 'next_cursor': None}})
    if host == 'clinicaltrials.gov':
        return httpx.Response(200, json={'studies': [TRIAL], 'totalCount': 1} if path.endswith('/studies') else TRIAL)
    if host == 'share.osf.io':
        assert request.url.params['cardSearchFilter[resourceType]'] == 'Registration'
        return httpx.Response(200, json={'data': [OSF], 'meta': {'total': 1}, 'links': {}})
    if host == 'api.osf.io':
        return httpx.Response(200, json={'data': {'id': 'a' * 24, 'attributes': {'schema': {'q1': 'Synthetic analysis question?'}}}}
                              if '/schemas/' in path else REGISTRATION)
    assert host == 'ops.epo.org'
    if path.endswith('/accesstoken'):
        assert request.headers['Authorization'].startswith('Basic ')
        return httpx.Response(200, json={'access_token': 'synthetic-token', 'expires_in': '1200'})
    assert request.headers['Authorization'] == 'Bearer synthetic-token'
    if '/search/' in path: return httpx.Response(200, content=PATENT)
    section = path.rsplit('/', 1)[-1]
    return httpx.Response(200, content=(f'<world-patent-data><{section} lang="en"><p>' +
        f'Synthetic {section}, not independent validation. ' * 25 + f'</p></{section}></world-patent-data>').encode())


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('EPO_OPS_KEY', 'synthetic-ops-key')
    monkeypatch.setenv('EPO_OPS_SECRET', 'synthetic-ops-secret')
    adapters._TOKENS.clear()
    workspace = tmp_path / 'run'; workspace.mkdir()
    packet = research_spec.compile_mapping({'question': plan()['question'],
        'retrieval': {'additional_sources': list(adapters.NAMES), 'target_papers': 1}})
    research_bundle.initialize(workspace, packet)
    engine.write(workspace / 'run.json', {'question': plan()['question'], 'minimum_papers': 1,
                                        'additional_sources': list(adapters.NAMES), 'status': 'researching'})
    engine.write(workspace / 'draft.json', plan(*adapters.NAMES))
    return workspace


def discover(workspace, handler=mock_provider, pages=1):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return engine.discover(workspace, workspace / 'draft.json', root=workspace.parent, client=client, pages=pages)


def test_defaults_unchanged_and_selected_sources_share_the_http_contract():
    plain = research_spec.compile_mapping({'question': 'Q?'})
    explicit_empty = research_spec.compile_mapping({'question': 'Q?', 'retrieval': {'additional_sources': []}})
    assert plain['resolved'] == explicit_empty['resolved']
    assert plain['spec_sha256'] == explicit_empty['spec_sha256']
    assert 'additional_sources' not in plain['resolved']['retrieval']
    schema = api_contract.document()['components']['schemas']['ResearchDeclaration']
    for sources in ([], ['clinicaltrials'], list(adapters.NAMES)):
        packet = research_spec.compile_mapping({'question': 'Q?', 'retrieval': {'additional_sources': sources}})
        assert research_spec.verify_compiled(packet) == packet
        jsonschema.Draft202012Validator(schema).validate(packet['resolved'])
    for value in ('osf', ['unknown'], ['osf', 'osf'], [False], None):
        with pytest.raises(research_spec.CompileError):
            research_spec.compile_mapping({'question': 'Q?', 'retrieval': {'additional_sources': value}})
    with pytest.raises(research_spec.CompileError):
        research_spec.compile_mapping({'question': 'Q?', 'retrieval': {'corpus': 'local_text',
            'local_text': {'directory': '/fixture'}, 'additional_sources': ['osf']}})


def test_federated_discovery_acquisition_and_hash_bound_records(workspace):
    progress = discover(workspace)
    assert progress['complete'] and progress['keyword_requests'] == 1
    assert {r['source']: r['status'] for r in progress['additional_sources']} == dict.fromkeys(adapters.NAMES, 'complete')
    with CandidateIndex(workspace) as index:
        works = list(index.records())
    assert len(works) == 3 and {w['source_kind'] for w in works} == set(adapters.KINDS.values())
    assert all(engine.SOURCE_ID.fullmatch(w['id']) for w in works)
    receipts = engine.acquire(workspace, [w['id'] for w in works], root=workspace.parent,
                              transport=httpx.MockTransport(mock_provider))
    assert {r['status'] for r in receipts} == {'source_retrieved'}
    for reference in engine.catalog(workspace):
        assert reference['full_text_status'] == 'record_requires_source_verification'
        raw = (workspace / reference['file']).read_bytes()
        text = (workspace / reference['text_file']).read_text()
        assert reference['body_sha256'] == hashlib.sha256(raw).hexdigest()
        assert reference['text_sha256'] == hashlib.sha256(text.encode()).hexdigest()
        passages = engine.source(workspace, reference['id'], limit=100)
        assert passages and all(text[p['start']:p['end']] == p['text'] for p in passages)
    all_metadata = '\n'.join(p.read_text() for p in workspace.rglob('*.json'))
    assert 'synthetic-ops-secret' not in all_metadata and 'synthetic-token' not in all_metadata
    statuses = engine.read(workspace / 'additional-sources.json')
    assert all(r['retrieved'] == 1 for r in statuses)


def test_provider_failure_does_not_suppress_other_sources_or_claim_completeness(workspace):
    def handler(request):
        if request.url.host == 'api.openalex.org': return httpx.Response(429, headers={'Retry-After': '3600'})
        if request.url.host == 'clinicaltrials.gov': return httpx.Response(503)
        return mock_provider(request)
    progress = discover(workspace, handler)
    assert not progress['complete']
    statuses = {r['source']: r for r in progress['additional_sources']}
    assert statuses['clinicaltrials']['status'] == 'failed'
    assert statuses['osf']['status'] == statuses['epo']['status'] == 'complete'
    assert 'HTTP 503' in statuses['clinicaltrials']['failures']
    assert 'q0' in progress['pending_query_ids'] and 'q1' in progress['pending_query_ids']


def test_ops_zero_total_is_complete_without_inventing_documents(workspace):
    def handler(request):
        if request.url.host == 'ops.epo.org' and '/search/' in request.url.path:
            return httpx.Response(200, content=b'<world-patent-data><biblio-search total-result-count="0"/></world-patent-data>')
        return mock_provider(request)
    progress = discover(workspace, handler)
    epo = next(s for s in progress['additional_sources'] if s['source'] == 'epo')
    assert epo['status'] == 'complete' and epo['candidates'] == 0 and epo['failures'] == []
    assert progress['queries']['q2']['matched'] == 0


@pytest.mark.parametrize('code', [403, 404, 429, 503])
def test_ops_rejection_stays_incomplete_and_retains_only_public_rate_headers(workspace, code):
    headers = {'X-Throttling-Control': 'ops: red', 'X-Rejection-Reason': 'RegisteredQuotaPerWeek',
               'X-RegisteredQuotaPerWeek-Used': '4294967296', 'Retry-After': '60',
               'Authorization': 'Bearer private-response-token', 'Set-Cookie': 'private-cookie'}
    def handler(request):
        if request.url.host == 'ops.epo.org' and '/search/' in request.url.path:
            return httpx.Response(code, headers=headers, text='private-error-body')
        return mock_provider(request)
    progress = discover(workspace, handler)
    statuses = {s['source']: s for s in progress['additional_sources']}
    assert statuses['epo']['status'] == 'failed' and statuses['epo']['candidates'] == 0
    assert statuses['epo']['failures'] == [f'HTTP {code}']
    assert statuses['clinicaltrials']['status'] == statuses['osf']['status'] == 'complete'
    query = progress['queries']['q2']
    assert query['cursor'] == '*' and query['pages'] == 0 and not query['complete']
    receipts = [json.loads(line) for line in (workspace / 'additional-source-requests.jsonl').read_text().splitlines()]
    receipt = next(r for r in receipts if r['source'] == 'epo')
    assert receipt['rate_headers'] == {k.lower(): v for k, v in headers.items()
                                      if k not in ('Authorization', 'Set-Cookie')}
    persisted = '\n'.join(p.read_text() for p in workspace.rglob('*')
                          if p.is_file() and p.suffix in ('.json', '.jsonl', '.xml'))
    assert all(secret not in persisted for secret in ('private-response-token', 'private-cookie', 'private-error-body'))


def test_only_enabled_sources_can_be_queried_and_history_cannot_change_provider(workspace):
    original = (workspace / 'draft.json').read_bytes()
    wrong = plan(*adapters.NAMES); wrong['queries'][1]['source'] = 'not-enabled'
    engine.write(workspace / 'draft.json', wrong)
    with pytest.raises(ValueError): discover(workspace)
    assert not (workspace / 'question-plan.json').exists()
    (workspace / 'draft.json').write_bytes(original)
    discover(workspace)
    committed = (workspace / 'question-plan.json').read_bytes()
    wrong = plan(*adapters.NAMES); wrong['queries'][1]['source'] = 'osf'; wrong['queries'][1]['search'] = 'different'
    engine.write(workspace / 'draft.json', wrong)
    with pytest.raises(ValueError, match='Change a query ID'): discover(workspace)
    assert (workspace / 'question-plan.json').read_bytes() == committed


def test_provider_pagination_and_result_caps_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv('EPO_OPS_KEY', 'synthetic-ops-key'); monkeypatch.setenv('EPO_OPS_SECRET', 'synthetic-ops-secret')
    def handler(request):
        response = mock_provider(request)
        if request.url.host == 'ops.epo.org' and '/search/' in request.url.path:
            response = httpx.Response(200, content=PATENT.replace(b'total-result-count="1"', b'total-result-count="2500"'))
        return response
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        _, rows, cursor, total, reason = adapters.Client(http, tmp_path).search('epo', 'ta=synthetic', '1901')
    assert rows and cursor is None and total == 2500 and '2,000' in reason
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        'data': [], 'links': {'next': 'https://private.invalid/trove/index-card-search?page[cursor]=bad'}}))) as http:
        with pytest.raises(ValueError, match='pagination link'):
            adapters.Client(http, tmp_path).search('osf', 'synthetic', '*')


def test_payload_and_resolvent_preserve_source_semantics_and_do_not_count_plans_as_papers(workspace):
    discover(workspace)
    with CandidateIndex(workspace) as index: works = list(index.records())
    engine.acquire(workspace, [w['id'] for w in works], root=workspace.parent,
                   transport=httpx.MockTransport(mock_provider))
    references = engine.catalog(workspace)
    reviews, evidence, claims = [], [], []
    for i, reference in enumerate(references, 1):
        passage = engine.source(workspace, reference['id'], limit=1)[0]
        reviews.append({'work_id': reference['id'], 'eligibility': 'supporting', 'passage_ids_read': [passage['id']],
            'investigated_sections': ['Synthetic complete source record'], 'substantive_review_note': 'Inspected this synthetic source record.'})
        evidence.append({'id': f'E{i}', 'passage_ids': [passage['id']], 'relation': 'qualifies', 'interpretation': 'Synthetic source attribution.'})
        claims.append({'id': f'C{i}', 'text': 'Synthetic attributed record statement.', 'kind': 'author_interpretation',
                       'evidence_ids': [f'E{i}'], 'study_result_ids': [], 'uncertainty': {'class': 'high', 'score': 3}, 'freshness': unknown()})
    engine.write(workspace / 'selection.json', {'selected_for_review_ids': [r['id'] for r in references]})
    engine.write(workspace / 'reviewed-papers.json', reviews)
    engine.write(workspace / 'scarcity.json', {'reason': 'Synthetic fixture contains registry records and a patent, no papers.'})
    engine.write(workspace / 'investigation.json', {'claims': claims, 'evidence': evidence, 'executive_summary': [], 'study_results': []})
    payload = engine.bind_payload(workspace)
    assert payload['mechanical_audit']['pass']
    assert payload['accounting']['substantively_reviewed_publications'] == 0
    assert payload['accounting']['additional_documents_reviewed'] == 3
    assert payload['accounting']['minimum_met'] is False
    engine.write(workspace / 'run.json', dict(engine.read(workspace / 'run.json'), status='complete'))
    view = coverage_export.export_bundle(workspace)
    assert view['coverage']['reviewed_papers'] == 0 and view['coverage']['additional_documents_reviewed'] == 3
    assert all('openalex.org/S' not in r['paper']['url'] for r in view['answer']['references'])
    assert {r['paper']['extra']['source_kind'] for r in view['answer']['references']} == set(adapters.KINDS.values())
    research_bundle.seal(workspace)
    handoff = bundle_connectors.export(workspace, 'resolvent', representation='expanded')
    schema = json.loads((Path(__file__).parent / 'fixtures/resolvent-submission.schema.json').read_text())
    jsonschema.Draft202012Validator(schema).validate(handoff)
    assert {s['kind'] for s in handoff['sources']} == {'publication', 'web'}
    assert len(handoff['method_metadata']['source_metadata']) == 3
    assert 'OSF study plans do not establish findings' in handoff['handoff']['summary']
    for source in handoff['sources']:
        locator = json.loads(source['locator'])
        assert locator['source_kind'] in adapters.KINDS.values()
        assert source['excerpt'] == (workspace / locator['text_file']).read_text()[locator['start']:locator['end']]
    assert all(e['independent'] is False for e in handoff['evidence'])
    from canary import compact_bundle
    from scripts.verify_resolvent_receiver import verify_archive
    import io
    (workspace / '.run-reservation').touch()
    manifest = compact_bundle.retain(workspace)
    archive = io.BytesIO(); research_bundle.archive(workspace, archive)
    compact = bundle_connectors.export(workspace, 'resolvent')
    assert verify_archive(manifest, archive.getvalue(), compact) == len(handoff['sources'])
    assert compact['sources'] == handoff['sources']
    for reference in view['answer']['references']:
        context = dashboard.source_context(workspace, reference['id'], key='fixture')
        assert context['scope'] == 'excerpts' and context['text_available']
        assert context['text_complete'] is False and context['source_bytes_retained'] is False
        assert 'Full-paper' not in context['warning']
        assert adapters.LIMITATIONS[reference['paper']['extra']['adapter']] in context['warning']


def test_ui_selects_sources_and_exposes_type_scope_and_failures(tmp_path):
    run_dashboard(tmp_path, {}, r'''
const launch=buildLaunch({q:'Q?',mode:'question',additional_sources:['clinicaltrials','osf']});
assert.deepEqual(launch.spec,{question:'Q?',retrieval:{additional_sources:['clinicaltrials','osf']}});
assert.equal(buildLaunch({q:'Q?',mode:'question'}).kind,'question');
assert.throws(()=>buildLaunch({q:'Q?',corpus:'local_text',additional_sources:['osf']}),/question research flow/);
assert.throws(()=>buildLaunch({q:'Q?',additional_sources:['unknown']}),/supported/);
const html=additionalSourcesHtml({coverage:{additional_sources:[{source:'osf',label:'OSF Registrations',status:'failed',candidates:0,retrieved:0,limitations:'Plans only.',failures:['HTTP 503']}]}});
assert.match(html,/OSF Registrations/);assert.match(html,/HTTP 503/);assert.match(html,/do not count/);
const ref={id:'Sfixture',paper:{title:'Synthetic plan',extra:{source_kind:'study_registration',adapter:'osf',local_file:'papers/fixture.json',source_metadata:{native_id:'<unsafe>'}}}};
const citation=qaCitation(ref);assert.match(citation,/Read source record/);assert.match(citation,/Saved record/);
assert.match(citation,/&lt;unsafe&gt;/);assert.doesNotMatch(citation,/Read complete paper/);
const passage={id:'Sfixture-p1',source_id:ref.id,kind:'fulltext',text:'Synthetic plan.',start:0,end:15,anchored:true};
const g={references:[ref],passages:[passage],evidence:[{id:'E1',paragraph_id:passage.id,quote:passage.text,anchored:true}],claims:[{id:'C1',text:'Synthetic intent.',evidence_ids:['E1']}]};
assert.match(evidenceInspector(g,'E1'),/Exact passage from the saved source record/);
assert.match(paragraphInspector(g,passage.id),/Exact saved source text/);
assert.match(claimRecordHtml(g,g.claims[0],''),/Source record passage/);
ref.source_retention='excerpts_only';
for(const html of [evidenceInspector(g,'E1'),paragraphInspector(g,passage.id)]) {
  assert.match(html,/Exact retained source excerpt/);
  assert.doesNotMatch(html,/retained paper excerpt/);
}
''')


def test_cli_and_http_submit_identical_additive_settings_without_client_provider_calls(tmp_path, monkeypatch, capsys):
    server = dashboard.make_server(0, root=tmp_path, registry=tmp_path / 'registry.json')
    received = []
    def start(packet, name='', brief=''):
        received.append(packet)
        return {'key': 'synthetic-source-submission', 'spec_sha256': packet['spec_sha256']}
    monkeypatch.setattr(server.RequestHandlerClass.supervisor, 'start_compiled', start)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        assert cli.main(['ask', 'Q?', '--add-source', 'clinicaltrials', '--add-source', 'osf',
                         '--port', str(server.server_port)]) == 0
        assert received[0]['resolved']['retrieval']['additional_sources'] == ['clinicaltrials', 'osf']
        assert json.loads(capsys.readouterr().out)['key'] == 'synthetic-source-submission'
        assert cli.main(['ask', 'Q?', '--port', str(server.server_port)]) == 0
        assert 'additional_sources' not in received[-1]['resolved']['retrieval']
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            compiled = client.post('/api/runs/compile', json={'spec': {'question': 'Q?',
                'retrieval': {'additional_sources': ['clinicaltrials', 'osf']}}})
            assert compiled.status_code == 200
            assert compiled.json()['resolved'] == received[0]['resolved']
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


def test_epo_missing_credentials_are_rejected_before_allocating_a_run(tmp_path, monkeypatch):
    monkeypatch.delenv('EPO_OPS_KEY', raising=False); monkeypatch.delenv('EPO_OPS_SECRET', raising=False)
    packet = research_spec.compile_mapping({'question': 'Q?', 'retrieval': {'additional_sources': ['epo']}})
    supervisor = dashboard.Supervisor(root=tmp_path, registry=tmp_path / 'registry.json')
    monkeypatch.setattr(supervisor, 'start', lambda *args, **kwargs: pytest.fail('Worker allocated without EPO credentials'))
    with pytest.raises(ValueError, match='EPO_OPS_KEY'):
        supervisor.start_compiled(packet)
    assert not (tmp_path / 'run-inputs').exists()


def test_clinical_pagination_retains_first_page_count_when_later_pages_omit_it(workspace):
    def handler(request):
        if request.url.host != 'clinicaltrials.gov': return mock_provider(request)
        if request.url.params.get('pageToken'):
            second = json.loads(json.dumps(TRIAL))
            second['protocolSection']['identificationModule']['nctId'] = 'NCT00000002'
            return httpx.Response(200, json={'studies': [second]})
        return httpx.Response(200, json={'studies': [TRIAL], 'totalCount': 2, 'nextPageToken': 'next-fixture-page'})
    first = discover(workspace, handler)
    assert first['queries']['q1']['matched'] == 2 and not first['queries']['q1']['complete']
    resumed = discover(workspace, handler)
    assert resumed['complete'] and resumed['queries']['q1']['pages'] == 2
    assert resumed['queries']['q1']['matched'] == 2
    assert next(s for s in resumed['additional_sources'] if s['source'] == 'clinicaltrials')['candidates'] == 2


def test_mixed_source_summary_counts_are_labeled_as_candidates(workspace):
    record=engine.read(workspace/'run.json')
    engine.write(workspace/'run.json',dict(record,kind='question',discovered=4))
    summary=dashboard.research_summary({'bundle':str(workspace)})
    assert summary['papers_count']==4 and summary['papers_count_scope']=='source_candidates'
    engine.write(workspace/'run.json',dict(record,kind='question',additional_sources=[],discovered=4))
    assert 'papers_count_scope' not in dashboard.research_summary({'bundle':str(workspace)})
