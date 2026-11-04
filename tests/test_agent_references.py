"""Agents receive allowlisted source rows; complete paper metadata stays in outputs."""
import json

import httpx
import jsonschema

from canary import (api_contract, correction_runtime, coverage_protocol as protocol, coverage_runtime as runtime,
                    research_tools, runs, tools_cli)
from canary.coverage_protocol import agent_reference
from canary.paper_metadata import snapshot
from test_coverage_evidence_ui import run_dashboard
from test_coverage_runtime import Transport, rich_plan, work
from test_research_bundle import api  # noqa: F401  (fixture)

TEXT = 'Synthetic methods and comparable results. ' * 30
RETRIEVED = '2026-11-04T10:00:00+00:00'


def rich_work(identifier='W1', authors=1):
    return dict(work(identifier), publication_date='2026-01-02', is_retracted=True, type='article',
                cited_by_count=7, fwci=1.5, counts_by_year=[{'year': 2026, 'cited_by_count': 7}],
                referenced_works=['https://openalex.org/W2'], study_designs=[{'display_name': 'Observational Study'}],
                authorships=[{'author': {'id': f'https://openalex.org/A{n}', 'display_name': f'Synthetic Author {n}',
                                         'orcid': f'https://orcid.org/0000-0000-0000-{n:04d}'},
                              'institutions': [{'id': f'https://openalex.org/I{n}', 'display_name': 'Synthetic Institute'}]}
                             for n in range(authors)])


def fetch(work, transport):
    return (TEXT.encode(), 'text/plain', 'https://example.org/' + work['id'].rsplit('/', 1)[-1]), False


def extract(raw, kind, work):
    return raw.decode(), 1, [], 'txt'


def discover(directory, works, root):
    runtime.write(directory / 'plan.json', dict(rich_plan(), question='Synthetic question?'))
    reply = {'results': works, 'meta': {'count': len(works), 'next_cursor': None}}
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply))) as client:
        runtime.discover(directory, directory / 'plan.json', root=root, client=client)


def validate_rows(rows):
    contract = api_contract.document()
    schema = contract['paths']['/api/runs/{key}/tools/catalog']['post']['responses']['200']['content']['application/json']['schema']
    jsonschema.validate(rows, dict(schema, components=contract['components']))


def test_agent_inputs_exclude_full_metadata_and_outputs_keep_it(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENALEX_API_KEY', 'synthetic-key')
    monkeypatch.delenv('RUN_KEY', raising=False)
    monkeypatch.delenv('CANARY_CANCEL_MARKER', raising=False)
    monkeypatch.setenv('CANARY_DATA_DIR', str(tmp_path / 'data'))
    workspace = tmp_path / 'research'
    seen = {}

    def session(directory, role, model, effort, prompt, shape, **kwargs):
        seen.setdefault('prompts', {}).setdefault(role, prompt)
        if role == 'research':
            discover(directory, [rich_work()], tmp_path)
            runtime.acquire(directory, ['W1'], fetch=fetch, extract=extract, transport=Transport())
            seen['catalog'] = research_tools.execute({'bundle': str(directory), 'status': 'researching'}, 'catalog', {}, tmp_path)
            passage = runtime.source(directory, 'W1')[0]
            runtime.write(directory / 'investigation.json', {
                'executive_summary': [{'text': 'Finding.', 'claim_ids': ['C1']}],
                'claims': [{'id': 'C1', 'text': 'Finding.', 'kind': 'reported_finding',
                            'uncertainty': {'class': 'moderate', 'score': 2, 'rationale': 'Scope.'},
                            'evidence_ids': ['E1'], 'study_result_ids': []}],
                'evidence': [{'id': 'E1', 'passage_ids': [passage['id']], 'relation': 'supports', 'interpretation': 'Reading.'}],
                'study_results': [], 'gaps': [], 'residue': [], 'contradictions': [], 'next_questions': [], 'caveats': []})
            return {}, {}
        seen['audit_input'] = json.loads((directory / 'audit-input.json').read_text())
        return {'audit_complete': True, 'claim_checks': [{'claim_id': 'C1', 'assessment': 'supported'}]}, {}

    monkeypatch.setattr(runtime, 'scientific_session', session)
    runtime.run('Synthetic question?', workspace, minimum_papers=1, executable='offline-codex')

    assert 'paper_metadata' not in seen['catalog'][0]
    assert 'paper_metadata' not in seen['audit_input']['references'][0]
    final = json.loads((workspace / 'payload.json').read_text())
    assert final['references'][0]['paper_metadata']['attributes']['is_retracted'] is True
    assert runtime.catalog(workspace)[0]['paper_metadata']['attributes']['publication_date'] == '2026-01-02'
    assert json.loads((workspace / 'references.json').read_text())[0]['paper_metadata']

    assert all(protocol.SOURCE_ROWS in seen['prompts'][role] for role in ('research', 'audit'))
    row = seen['catalog'][0]
    validate_rows(seen['catalog'])
    assert (row['publication_date'], row['type'], row['is_retracted']) == ('2026-01-02', 'article', True)
    assert not {'cited_by_count', 'fwci', 'study_designs', 'body_sha256', 'url'} & set(row)
    assert seen['audit_input']['references'] == seen['catalog']
    assert [json.loads(line) for line in (workspace / 'catalog.jsonl').read_text().splitlines()] == seen['catalog']
    assert runtime.read(workspace / 'run.json')['agent_paper_metadata_fields'] == ['publication_date', 'type', 'is_retracted']


def test_fifty_hundred_author_rows_fit_one_printed_tool_output(api, tmp_path, monkeypatch, capsys):
    server, _, _ = api
    monkeypatch.setenv('OPENALEX_API_KEY', 'synthetic-key')
    monkeypatch.delenv('CANARY_API_URL', raising=False)
    workspace = tmp_path / 'sizing'; workspace.mkdir()
    runtime.write(workspace / 'run.json', {'question': 'Synthetic question?', 'kind': 'question', 'status': 'researching'})
    title = 'Synthetic multi-site measurement of delayed retrieval with matched comparators and repeated follow-up ' * 2
    works = [dict(rich_work(f'W{4100000000 + n}', authors=100), display_name=title.strip()) for n in range(50)]
    discover(workspace, works, tmp_path)
    ids = [w['id'].rsplit('/', 1)[-1] for w in works]
    acquire = runtime.acquire
    monkeypatch.setattr(runtime, 'acquire', lambda directory, selected, **options:
                        acquire(directory, selected, fetch=fetch, extract=extract, transport=Transport(), **options))
    acquired = research_tools.execute({'bundle': str(workspace), 'status': 'researching'}, 'acquire', {'ids': ids}, tmp_path)
    assert acquired['attempted'] == acquired['sources_available'] == 50
    validate_rows([r['reference'] for r in acquired['receipts']])

    runs.register_start('sizing', 'fixture', '', bundle=str(workspace), status='researching',
                        path=server.RequestHandlerClass.supervisor.registry)
    capsys.readouterr()  # Discard engine progress lines; measure only the tool's printed result.
    assert tools_cli.main(['--run', 'sizing', '--port', str(server.server_port), 'catalog']) == 0
    printed = capsys.readouterr().out
    assert len(printed.encode('utf-8')) <= 40_000
    rows = json.loads(printed)
    validate_rows(rows)
    assert len(rows) == 50 and all(len(r['authors']) == 3 and r['authors_total'] == 100 for r in rows)
    complete = runtime.read(workspace / 'download-receipts' / (ids[0] + '.json'))['reference']
    assert len(complete['authors']) == 100 and len(complete['paper_metadata']['attributes']['authorships']) == 100


def test_audit_and_correction_inputs_carry_rows_while_final_payload_keeps_metadata(tmp_path, monkeypatch):
    from test_corrections import audit_for, prepared, response
    monkeypatch.delenv('RUN_KEY', raising=False)
    monkeypatch.delenv('CANARY_CANCEL_MARKER', raising=False)
    _, manifest = prepared(tmp_path, rounds=1)
    receipt = runtime.read(tmp_path / 'download-receipts/W1.json')
    metadata = snapshot(rich_work(), RETRIEVED)
    receipt['reference']['paper_metadata'] = metadata
    runtime.write(tmp_path / 'download-receipts/W1.json', receipt)
    payload = runtime.bind_payload(tmp_path)
    seen = {'audit': []}

    def scientific(workspace, role, model, effort, prompt, shape, **kwargs):
        directory = workspace / 'rounds' / f"{kwargs['round_index']:03d}"
        if role == 'audit':
            seen['audit'].append(runtime.read(workspace / 'audit-input.json'))
            data = audit_for(seen['audit'][-1], 'correction_needed' if kwargs['round_index'] == 0 else 'supported')
        else:
            seen['correction'] = runtime.read(directory / 'correction-input.json')
            data = response(seen['correction']['payload'])
        result = {'thread_id': role, 'model': model, 'reasoning_effort': effort, 'exit_code': 0, 'seconds': 1,
                  'usage': {'available': False}}
        runtime.write(directory / (role + '.json'), data)
        runtime.write(directory / (role + '-receipt.json'), result)
        return data, result

    monkeypatch.setattr(runtime, 'scientific_session', scientific)
    final = correction_runtime.verify(tmp_path, payload, manifest, 'canary tools', 'synthetic-codex')
    assert len(seen['audit']) == 2 and seen['correction']
    expected = [agent_reference(receipt['reference'])]
    assert expected[0]['is_retracted'] is True and 'paper_metadata' not in expected[0]
    for subject in [*seen['audit'], seen['correction']['payload']]:
        assert subject['references'] == expected
    assert final['references'][0]['paper_metadata'] == metadata
    assert runtime.read(tmp_path / 'payload.json')['references'][0]['paper_metadata'] == metadata


def test_rows_keep_source_fields_and_never_invent_metadata():
    base = {'id': 'W1', 'title': 'Synthetic', 'doi': None, 'year': 2026, 'authors': ['A', None, 'C', 'D'],
            'file': 'papers/W1.txt', 'text_file': 'texts/W1.txt', 'body_sha256': 'a' * 64, 'text_sha256': 'b' * 64,
            'pages': 0, 'blank_pages': [], 'text_chars': 1260, 'preview_marker_detected': False,
            'full_text_status': 'requires_source_verification', 'url': 'https://example.org/W1'}
    local = dict(base, adapter='local_text', local_source={'file': 'a.txt', 'sha256': 'c' * 64, 'bytes': 1260},
                 openalex_id=None, imported_at=RETRIEVED, full_text_status='local_text_requires_source_verification')
    record = dict(base, adapter='clinicaltrials', source_kind='clinical_trial_registration', record_url='https://example.org/r',
                  source_metadata={'native_id': 'NCT0', 'record_date': None}, full_text_status='record_requires_source_verification')
    unknown = dict(base, paper_metadata=snapshot({'id': 'https://openalex.org/W1', 'is_retracted': False}, RETRIEVED))
    rows = [agent_reference(r) for r in (base, local, record, unknown, {'id': 'W9'})]
    validate_rows(rows)
    plain, local_row, record_row, unknown_row, historical = rows
    assert plain['authors'] == ['A', None, 'C'] and plain['authors_total'] == 4
    assert not {'publication_date', 'type', 'is_retracted', 'text_file', 'url'} & set(plain)
    assert {k: local_row[k] for k in ('adapter', 'local_source', 'openalex_id', 'imported_at')} == {
        k: local[k] for k in ('adapter', 'local_source', 'openalex_id', 'imported_at')}
    assert {k: record_row[k] for k in ('adapter', 'source_kind', 'record_url', 'source_metadata')} == {
        k: record[k] for k in ('adapter', 'source_kind', 'record_url', 'source_metadata')}
    assert (unknown_row['is_retracted'], unknown_row['publication_date'], unknown_row['type']) == (False, None, None)
    assert historical['authors'] is None and historical['authors_total'] is None and 'is_retracted' not in historical


def test_source_inspector_states_agent_source_rows_without_overstating_exposure(tmp_path):
    graph = {'references': [{'id': 'W1', 'paper': {'title': 'Synthetic', 'extra': {'paper_metadata': snapshot(rich_work(), RETRIEVED)}}}]}
    run_dashboard(tmp_path, graph, r'''
const ref=graph.references[0];
assert.match(qaCitation(ref),/This run did not record which of these fields agent source rows carried/);
assert.doesNotMatch(qaCitation(ref),/source rows (did not )?include(d)? this flag/);
S.selected='synthetic';
S.research={synthetic:{run:{agent_paper_metadata_fields:['publication_date','type','is_retracted']}}};
const html=qaCitation(ref);
assert.match(html,/Review claims using this paper\. Agent source rows included this flag\./);
assert.match(html,/\(catalog, audit input and correction input\) carried only <code>publication_date<\/code>, <code>type<\/code>, <code>is_retracted<\/code> from this snapshot/);
assert.match(html,/During the run, the complete record, including citation counts, remained readable in download receipts and references\.json/);
assert.doesNotMatch(html,/agents received only/);
S.research.synthetic.run.agent_paper_metadata_fields=['publication_date'];
assert.match(qaCitation(ref),/Agent source rows did not include this flag/);
''')
