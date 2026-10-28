"""Bounded metadata reads must preserve the complete candidate net and provenance."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import httpx
import jsonschema
import pytest

from canary import api_contract, coverage_export, coverage_runtime as runtime
from canary.candidate_index import CandidateIndex, NAME, legacy_records
from test_coverage_runtime import work, rich_plan, extraction, Transport
from test_coverage_evidence_ui import run_dashboard


def test_first_seen_metadata_order_and_all_query_links_survive_repeated_pages(tmp_path):
    with CandidateIndex(tmp_path) as index:
        index.add_page([work('W2'),work('W1')], 'q1')
        changed = dict(work('W2'), display_name='Changed duplicate metadata')
        index.add_page([changed,work('W3'),work('W2')], 'q2')
        index.add_page([work('W2')], 'q2')
        rows = list(index.records())
        assert [r['id'].split('/')[-1] for r in rows] == ['W2','W1','W3']
        assert [r['fresh_search_rank'] for r in rows] == [1,2,3]
        assert rows[0]['query_ids'] == ['q1','q2']
        assert rows[0]['display_name'] == work('W2')['display_name']
        index.export()
        selected = index.selected(['W2'])
        assert list(selected) == ['W2']
        assert selected['W2'] == rows[0]
    assert not (tmp_path/'works.json').exists()
    candidates = [json.loads(line) for line in (tmp_path/'candidates.jsonl').read_text().splitlines()]
    assert [r['id'] for r in candidates] == ['W2','W1','W3']
    assert candidates[0]['abstract'] == 'synthetic abstract'
    assert candidates[0]['query_ids'] == ['q1','q2']


def test_acquisition_decodes_only_selected_metadata_and_preserves_complete_candidate_view(tmp_path):
    with CandidateIndex(tmp_path) as index:
        index.add_page([work('W1'),work('W2')], 'q1')
        index.export()
        # An unread record must not be decoded by an unrelated acquisition batch.
        with index.db:
            index.db.execute("UPDATE works SET record='not valid JSON' WHERE id='W2'")
    before = (tmp_path/'candidates.jsonl').read_bytes()
    receipts = runtime.acquire(tmp_path, ['W1'], transport=Transport(), extract=extraction,
        fetch=lambda *args: ((b'fixture source', 'application/pdf', 'https://example.org/fixture'), False))
    assert receipts[0]['status'] == 'source_retrieved'
    assert (tmp_path/'candidates.jsonl').read_bytes() == before
    exported = coverage_export.export_bundle(tmp_path)
    assert exported['coverage']['discovered_candidates'] == 2
    assert exported['run']['discovered'] == 2


def test_selected_ids_are_not_limited_by_sqlite_parameter_batch_size(tmp_path):
    with CandidateIndex(tmp_path) as index:
        index.add_page((work('W'+str(n)) for n in range(1,1101)), 'q1')
        selected = index.selected(['W'+str(n) for n in range(1,1101)])
        assert len(selected) == 1100
        assert selected['W1100']['fresh_search_rank'] == 1100


def test_legacy_array_import_is_streamed_lossless_and_safe_for_concurrent_readers(tmp_path):
    rows = [dict(work('W1'), display_name=('Δ "quoted" \\ line\n'*15000)), work('W2')]
    original = json.dumps(rows, ensure_ascii=False, indent=2).encode()
    (tmp_path/'works.json').write_bytes(original)
    assert list(legacy_records(tmp_path/'works.json')) == rows
    def read():
        with CandidateIndex(tmp_path) as index:
            return index.selected(['W1'])['W1']
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(row==rows[0] for row in pool.map(lambda _:read(),range(4)))
    assert hashlib.sha256((tmp_path/'works.json').read_bytes()).digest() == hashlib.sha256(original).digest()


@pytest.mark.parametrize('body', ['[{"id":"W1"},', '[{"id":"W1"},]', '[{"id":"W1"}] trailing', '{}'])
def test_invalid_legacy_array_never_publishes_a_partial_index(tmp_path, body):
    (tmp_path/'works.json').write_text(body)
    with pytest.raises(ValueError):
        CandidateIndex(tmp_path)
    assert not (tmp_path/NAME).exists()
    assert (tmp_path/'works.json').read_text() == body


def test_page_transaction_rolls_back_if_any_returned_record_is_invalid(tmp_path):
    with CandidateIndex(tmp_path) as index:
        index.add_page([work('W1')], 'q1')
        with pytest.raises(ValueError):
            index.add_page([work('W2'), {'id':'../../invalid'}], 'q2')
        assert index.count() == 1 and index.selected(['W2']) == {}


def test_discovery_contract_and_frontend_show_complete_net_without_pruning(tmp_path):
    plan = tmp_path/'draft.json'; runtime.write(plan,rich_plan())
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,
        json={'results':[work('W1'),work('W2')], 'meta':{'next_cursor':None}}))) as client:
        progress = runtime.discover(tmp_path/'research',plan,root=tmp_path,client=client)
    metadata = progress['candidate_metadata']
    assert metadata == {'storage':'sqlite','records':2,'acquisition_lookup':'selected_ids'}
    jsonschema.validate(metadata,api_contract.document()['components']['schemas']['CandidateMetadata'])
    result = coverage_export.export_bundle(tmp_path/'research')
    assert result['coverage']['candidate_metadata'] == metadata
    data = {'coverage':result['coverage'],'run':result['run']}
    run_dashboard(tmp_path, data, r'''
const before=JSON.stringify(graph),html=coverageSelectionHtml(graph);
assert.match(html,/All 2 candidate records were available for relevance assessment/);
assert.match(html,/Paper acquisition reads only selected records/);
assert.equal(JSON.stringify(graph),before);
''')
