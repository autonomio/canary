"""Issue #159: a clarification stop is its own terminal outcome; a failure says what went wrong.

Synthetic research sessions only; no provider inference or scientific evidence.
"""
from contextlib import contextmanager
import io
import json
import sys
import threading

import httpx
import pytest
from jsonschema import Draft202012Validator, ValidationError

from canary import api_contract, cli, coverage_export, coverage_protocol as protocol, coverage_runtime, dashboard
from canary import research_bundle, research_spec, runs
from canary.mcp_server import tool_catalog
from test_coverage_evidence_ui import run_dashboard
from test_research_bundle import DECLARATION

CLARIFICATION = {'question': 'Which product and which outcome measure should be compared?',
                 'reason': 'The question names neither the product nor the measure.'}


def investigation(clarification, summary=()):
    return {'executive_summary': [{'text': text, 'claim_ids': []} for text in summary], 'claims': [], 'evidence': [],
            'study_results': [], 'contradictions': [], 'gaps': [], 'residue': [], 'next_questions': [], 'caveats': [],
            'scope_note': 'Synthetic stop before research.', 'clarification': clarification}


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """Run the compiled worker with a synthetic research session that returns `record`."""
    for name in ('RUN_KEY', 'CANARY_CANCEL_MARKER', 'CANARY_API_URL', 'CANARY_COMPACT_OUTPUT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(coverage_runtime.shutil, 'which', lambda _: sys.executable)
    staged = tmp_path / 'staged.json'
    research_bundle.write_json(staged, research_spec.compile_research(DECLARATION))
    roles = []
    def start(record, workspace):
        def session(directory, role, *args, **kwargs):
            roles.append(role)
            coverage_runtime.write(directory / 'investigation.json', record)
            return record, {'exit_code': 0}
        monkeypatch.setattr(coverage_runtime, 'scientific_session', session)
        return research_bundle.run_compiled(staged, workspace)
    return start, roles


def finish(tmp_path, workspace, key, exit_code):
    """Register the run and finish it exactly as the service does when its worker exits."""
    registry = tmp_path / 'registry.json'
    runs.register_start(key, 'Synthetic run', '', 'question', str(workspace), path=registry)
    class Worker:
        def wait(self): return exit_code
    dashboard.Supervisor(registry=registry, root=tmp_path)._wait(key, Worker(), io.StringIO())
    return registry


@contextmanager
def serve(tmp_path, registry, key):
    """Read one run through the real HTTP service and validate every response against its contract."""
    server = dashboard.make_server(0, registry=registry, root=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', timeout=20, trust_env=False) as client:
            contract = client.get('/openapi.json').json()
            seen = {'contract': contract, 'port': server.server_port,
                    'row': client.get('/api/runs/' + key).json(),
                    'listed': next(r for r in client.get('/api/runs').json()['runs'] if r['key'] == key),
                    'research': client.get('/api/runs/' + key + '/research').json(),
                    'log': next(r for r in client.get('/api/log').json()['runs'] if r['key'] == key)}
        for path, value in (('/api/runs/{key}', seen['row']), ('/api/runs/{key}/research', seen['research'])):
            schema = contract['paths'][path]['get']['responses']['200']['content']['application/json']['schema']
            Draft202012Validator(dict(schema, components=contract['components'])).validate(value)
        # The agent relay publishes the same typed run record as its canary_run output.
        Draft202012Validator(tool_catalog(contract)['canary_run']['tool'].outputSchema).validate(seen['row'])
        yield seen
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


def test_research_contract_types_the_clarification_stop():
    schema = protocol.investigation_schema()
    assert 'clarification' in schema['required']
    Draft202012Validator(schema).validate(investigation(None))
    Draft202012Validator(schema).validate(investigation(CLARIFICATION))
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(investigation({'question': CLARIFICATION['question']}))
    prompt = protocol.research_prompt('Is it better?', 'canary', 50)
    assert 'clarification {question, reason}' in prompt and 'Otherwise clarification is null.' in prompt
    padded = {'question': '  ' + CLARIFICATION['question'] + '\n', 'reason': CLARIFICATION['reason'] + ' '}
    assert protocol.clarification_request(investigation(padded)) == CLARIFICATION
    for value in (None, {}, {'question': ' '}, {'question': 3, 'reason': 'x'}, 'Which product?'):
        assert protocol.clarification_request(investigation(value)) is None
    assert protocol.clarification_request(None) is None
    published = api_contract.schemas()['Clarification']
    assert {k: v for k, v in published.items() if k != 'description'} == protocol.clarification_schema()


@pytest.mark.parametrize('compact', [False, True])
def test_clarification_stop_is_terminal_not_failed_across_api_cli_agent_and_dashboard(tmp_path, monkeypatch, capsys, worker, compact):
    start, roles = worker
    if compact:
        monkeypatch.setenv('CANARY_COMPACT_OUTPUT', '1')
    workspace = tmp_path / 'run'
    # The worker returns normally, so the service sees exit code 0; no audit session starts.
    assert start(investigation(CLARIFICATION, summary=[CLARIFICATION['question']]), workspace) == {'clarification': CLARIFICATION}
    assert roles == ['research']
    assert research_bundle.read(workspace)['execution']['status'] == 'needs_clarification'
    assert research_bundle.read(workspace)['execution']['answered'] is False
    registry = finish(tmp_path, workspace, 'clarify', 0)
    stored = runs.get('clarify', registry)
    assert stored['status'] == 'needs_clarification' and stored['clarification'] == CLARIFICATION
    with serve(tmp_path, registry, 'clarify') as seen:
        assert cli.main(['runs', 'show', 'clarify', '--port', str(seen['port'])]) == 0
        shown = json.loads(capsys.readouterr().out)
    status = seen['contract']['components']['schemas']['Run']['properties']['status']
    assert 'needs_clarification' in status['description']
    for record in (seen['row'], seen['listed'], shown):
        assert record['status'] == 'needs_clarification' and record['clarification'] == CLARIFICATION
        assert record['answered'] is False and record.get('failure') is None and record['audit_status'] == 'not_run'
    assert seen['log']['status'] == 'needs_clarification' and seen['log']['answered'] is False
    research = seen['research']['research']
    assert research['run']['clarification'] == CLARIFICATION and research['answer']['audit']['status'] == 'not_run'
    if not compact:
        assert not (workspace / 'payload.json').exists() and not (workspace / 'audit.json').exists()
    run_dashboard(tmp_path, {'row': seen['row'], 'listed': seen['listed'], 'research': research}, r'''
const row=graph.row, research=graph.research, [cls,label]=statusInfo(row);
assert.equal(cls,'neutral');assert.equal(label,'Needs clarification');assert.equal(active(row),false);
assert.equal(hasReport(row),false);
const html=answerHtml(row,research);
assert.match(html,/Clarification needed/);
assert.ok(html.includes(esc(row.clarification.question)) && html.includes(esc(row.clarification.reason)));
assert.match(html,/No findings were bound or audited/);
assert.match(html,/data-action="retry-research"/);
assert.match(coverageAuditHtml(research),/Independent audit · not run/);
const elements={};
global.document={activeElement:null,getElementById:id=>elements[id]||(elements[id]={textContent:'',innerHTML:'',querySelectorAll:()=>[],contains:()=>false})};
assert.equal(graph.listed.research_summary.has_synthesis,true);  // the recorded summary text is not an answer
S.runs=[graph.listed];renderLibrary();
const list=elements['research-list'].innerHTML;
assert.ok(list.includes(esc('Clarifying question: '+row.clarification.question)));
assert.match(list,/Needs clarification/);
assert.doesNotMatch(list,/Answer available|Needs attention/);
''')


def test_research_ending_before_discovery_fails_with_its_recorded_reason(tmp_path, worker):
    start, roles = worker
    workspace = tmp_path / 'run'
    # A summary-only stop without the typed clarification remains an incomplete run.
    with pytest.raises(ValueError, match='before discovery committed a question plan'):
        start(investigation(None, summary=['Which product should be compared?']), workspace)
    assert roles == ['research']
    recorded = json.loads((workspace / 'run.json').read_text())
    assert recorded['status'] == 'failed' and recorded['audit_status'] == 'not_run'
    assert recorded['failure'].startswith('ValueError: Research ended before discovery committed a question plan')
    assert recorded['answered'] is False  # claim-free summary text from a stopped run is not an answer
    registry = finish(tmp_path, workspace, 'stopped', 1)
    with serve(tmp_path, registry, 'stopped') as seen:
        row = seen['row']
    assert row['status'] == 'failed' and row.get('clarification') is None
    assert row['failure'] == recorded['failure']
    assert row['answered'] is False and seen['listed']['answered'] is False and seen['log']['answered'] is False
    run_dashboard(tmp_path, {'row': row, 'listed': seen['listed']}, r'''
const issue=diagnoseRun(graph.row,{timeline:[],console:{lines:['TypeError: unrelated later log line']}});
assert.equal(issue.title,'The run recorded why it stopped');
assert.equal(issue.text,graph.row.failure);
assert.equal(issue.recorded,'the run record');
assert.equal(statusInfo(graph.row)[0],'attention');
assert.equal(graph.listed.research_summary.has_synthesis,true);
const elements={};
global.document={activeElement:null,getElementById:id=>elements[id]||(elements[id]={textContent:'',innerHTML:'',querySelectorAll:()=>[],contains:()=>false})};
S.runs=[graph.listed];renderLibrary();
assert.doesNotMatch(elements['research-list'].innerHTML,/Answer available/);
''')


def test_a_stopped_run_never_reports_a_pending_audit(tmp_path):
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    claim = {'id': 'C1', 'text': 'Committed attributed finding.', 'evidence_ids': []}
    coverage_runtime.write(bundle / 'investigation.json', dict(investigation(None), claims=[claim]))
    run = {'kind': 'question', 'execution_mode': coverage_export.EXECUTION_MODE, 'question': 'Synthetic?',
           'status': 'researching', 'generated_at': '2026-11-04T00:00:00+00:00'}
    coverage_runtime.write(bundle / 'run.json', run)
    progress = coverage_export.export_bundle(bundle)  # the last progress export before the worker dies
    assert progress['run']['audit_status'] == 'pending' and progress['answer']['source_checks'][0]['reason'] == 'Audit pending.'
    registry = tmp_path / 'registry.json'
    runs.register_start('dead', 'Synthetic run', '', 'question', str(bundle), path=registry)
    with serve(tmp_path, registry, 'dead') as active:
        assert active['row']['audit_status'] == 'pending'
        assert active['research']['research']['answer']['audit']['status'] == 'pending'
    class Died:
        def wait(self): return 1
    dashboard.Supervisor(registry=registry, root=tmp_path)._wait('dead', Died(), io.StringIO())
    assert json.loads((bundle / 'run.json').read_text())['status'] == 'in_progress'  # no final export happened
    with serve(tmp_path, registry, 'dead') as seen:
        pass
    assert seen['row']['status'] == 'failed' and seen['row']['audit_status'] == 'not_run'
    assert seen['log']['audit_status'] == 'not_run' and seen['log']['answered'] is True  # committed claim kept
    research = seen['research']['research']
    assert research['answer']['audit']['status'] == 'not_run' and research['run']['audit_status'] == 'not_run'
    # A worker that records its own stop exports the same state, including each claim's audit reason.
    coverage_runtime.write(bundle / 'run.json', dict(run, status='failed', failure='RuntimeError: Synthetic stop'))
    stopped = coverage_export.export_bundle(bundle)
    assert stopped['run']['audit_status'] == 'not_run' and stopped['run']['answered'] is True
    assert stopped['answer']['source_checks'][0]['reason'] == 'No independent audit was run.'
    run_dashboard(tmp_path, {'research': research, 'row': seen['row']}, r'''
assert.match(coverageAuditHtml(graph.research),/Independent audit · not run/);
assert.match(technicalHtml(graph.row,graph.research),/<dt>Audit<\/dt><dd>not run<\/dd>/);
''')


def test_active_run_audit_stays_pending(tmp_path):
    (tmp_path / 'run.json').write_text(json.dumps({'kind': 'question', 'question': 'Synthetic?', 'status': 'researching'}))
    assert coverage_export.export_bundle(tmp_path)['run']['audit_status'] == 'pending'
