"""Metadata observations survive discovery, API projection and downstream retention."""
import json
from pathlib import Path

import httpx
import jsonschema

from canary import (api_contract, bundle_connectors, bundle_encoding, compact_bundle, coverage_export,
                    coverage_runtime as runtime, dashboard, research_bundle, research_spec)
from canary.paper_metadata import snapshot
from test_coverage_evidence_ui import run_dashboard
from test_coverage_runtime import Transport, rich_plan, work


def rich_work():
    return dict(work('W1'), publication_date='2026-01-02', cited_by_count=0,
        counts_by_year=[{'year': 2026, 'cited_by_count': 0}], fwci=0.0,
        citation_normalized_percentile={'value': 0.0, 'is_in_top_10_percent': False},
        is_retracted=True, type='article', language='en',
        authorships=[{'author': {'id': 'https://openalex.org/A1', 'display_name': 'A. Author',
                                'orcid': 'https://orcid.org/0000-0000-0000-0001'},
                     'institutions': [{'id': 'https://openalex.org/I1', 'display_name': 'Institute'}]}],
        primary_location={'source': {'display_name': 'Journal'}, 'version': 'publishedVersion'},
        best_oa_location={'license': 'cc-by', 'version': 'acceptedVersion'},
        open_access={'is_oa': True, 'oa_status': 'green'}, ids={'pmid': 'https://pubmed.ncbi.nlm.nih.gov/1'},
        funders=[{'id': 'https://openalex.org/F1', 'display_name': 'Funder'}],
        awards=[{'funder_award_id': 'Grant-1'}], referenced_works=['https://openalex.org/W2'],
        referenced_works_count=1, topics=[{'display_name': '<unsafe topic>'}],
        study_designs=[{'display_name': 'Observational Study'}])


def test_snapshot_keeps_unknown_distinct_from_zero_false_and_empty():
    raw = {'id': 'https://openalex.org/W1', 'cited_by_count': 0, 'is_retracted': False,
           'referenced_works': [], 'authorships': [{'author': {'display_name': 'Original'}}]}
    metadata = snapshot(raw, '2026-11-04T10:00:00+00:00')
    fields = metadata['attributes']
    assert fields['cited_by_count'] == 0 and fields['is_retracted'] is False
    assert fields['referenced_works'] == [] and fields['funders'] is None
    assert fields['publication_date'] is None and fields['fwci'] is None
    raw['authorships'][0]['author']['display_name'] = 'Mutated'
    assert fields['authorships'][0]['author']['display_name'] == 'Original'
    assert snapshot({'id': raw['id']}, metadata['retrieved_at'])['attributes']['cited_by_count'] is None


def test_discovery_to_downstream_and_compact_retention_preserves_paper_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENALEX_API_KEY', 'synthetic-key')
    packet = research_spec.compile_research('question: Synthetic question?\nretrieval: {target_papers: 1}')
    research_bundle.initialize(tmp_path, packet)
    (tmp_path / '.run-reservation').touch()
    runtime.write(tmp_path / 'run.json', {'question': 'Synthetic question?', 'kind': 'question',
                                        'status': 'complete', 'minimum_papers': 1})
    plan = dict(rich_plan(), question='Synthetic question?')
    runtime.write(tmp_path / 'plan.json', plan)
    requests = []
    def handler(request):
        requests.append(request)
        fields = request.url.params['select'].split(',')
        assert {'cited_by_count', 'publication_date', 'counts_by_year', 'is_retracted', 'funders',
                'study_designs', 'authorships', 'referenced_works'} <= set(fields)
        return httpx.Response(200, json={'results': [rich_work()], 'meta': {'count': 1, 'next_cursor': None}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        runtime.discover(tmp_path, tmp_path / 'plan.json', root=tmp_path, client=client)
    assert len(requests) == 1
    text = 'Synthetic methods and comparable results. ' * 30
    def fetch(work, transport):
        return (text.encode(), 'text/plain', 'https://example.org/paper'), False
    def extract(raw, kind, work):
        return raw.decode(), 1, [], 'txt'
    runtime.acquire(tmp_path, ['W1'], fetch=fetch, extract=extract, transport=Transport())
    reference = runtime.catalog(tmp_path)[0]
    metadata = reference['paper_metadata']
    receipt = runtime.read(tmp_path / 'openalex-requests.json')[0]
    assert metadata['retrieved_at'] == receipt['requested_at']
    assert metadata['provider_id'] == 'https://openalex.org/W1'
    assert metadata['attributes']['cited_by_count'] == 0
    assert metadata['attributes']['is_retracted'] is True
    passage = runtime.source(tmp_path, 'W1')[0]
    raw = {'executive_summary': [{'text': 'Synthetic finding.', 'claim_ids': ['C1']}],
           'claims': [{'id': 'C1', 'text': 'A synthetic attributed finding.', 'kind': 'reported_finding',
                       'uncertainty': {'class': 'moderate', 'score': 2, 'rationale': 'Synthetic scope.'},
                       'evidence_ids': ['E1'], 'study_result_ids': []}],
           'evidence': [{'id': 'E1', 'passage_ids': [passage['id']], 'relation': 'supports',
                         'interpretation': 'Synthetic interpretation.'}],
           'study_results': [], 'gaps': [], 'residue': [], 'contradictions': [],
           'next_questions': [], 'caveats': []}
    runtime.write(tmp_path / 'investigation.json', raw)
    payload = runtime.bind_payload(tmp_path)
    assert payload['references'][0]['paper_metadata'] == metadata
    graph = coverage_export.export_bundle(tmp_path)['answer']
    assert graph['references'][0]['paper']['extra']['paper_metadata'] == metadata
    research_bundle.seal(tmp_path)
    exported = bundle_connectors.export(tmp_path, 'resolvent', representation='expanded')
    schema = json.loads((Path(__file__).parent / 'fixtures/resolvent-submission.schema.json').read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(exported)
    assert exported['method_metadata']['paper_metadata'] == {'W1': metadata}
    contract = api_contract.schemas()
    jsonschema.validate(metadata, contract['PaperMetadata'])
    export_schema = dict(contract['ResolventExport'], components={'schemas': contract})
    jsonschema.validate(exported, export_schema)
    source = exported['sources'][0]
    assert source['date'] == '2026-01-02'
    assert json.loads(source['locator'])['paper_metadata_id'] == 'W1'
    assert exported['claims'][0]['evidence_ids'] == [exported['evidence'][0]['id']]
    assert exported['evidence'][0]['source_ids'] == [source['id']]
    encoded = bundle_connectors.export(tmp_path, 'resolvent')
    assert bundle_encoding.expand(encoded) == exported
    compact_bundle.retain(tmp_path)
    retained = bundle_connectors.export(tmp_path, 'resolvent', representation='expanded')
    assert retained['method_metadata']['paper_metadata'] == {'W1': metadata}
    assert retained['sources'] == exported['sources']
    assert not (tmp_path / 'references.json').exists()
    view = dashboard.research_bundle(tmp_path)
    assert view['answer']['references'][0]['paper']['extra']['paper_metadata'] == metadata
    run_dashboard(tmp_path, view['answer'], r'''
const ref=graph.references[0], before=JSON.stringify(ref), html=qaCitation(ref);
assert.match(html,/0 citations/);
assert.match(html,/Retraction reported by OpenAlex/);
assert.match(html,/2026-01-02/);
assert.match(html,/retrieved 20/);
assert.match(html,/not claim reliability/);
assert.match(html,/&lt;unsafe topic&gt;/);
assert.doesNotMatch(html,/<unsafe topic>/);
assert.match(sourceInspector(graph,{sourceId:'W1',scope:'paper'}),/Paper metadata/);
ref.paper.extra.paper_metadata.attributes.is_retracted=false;
assert.doesNotMatch(qaCitation(ref),/Retraction reported/);
assert.match(qaCitation(ref),/No retraction flagged by provider/);
ref.paper.extra.paper_metadata.attributes.cited_by_count=null;
ref.paper.extra.paper_metadata.attributes.is_retracted=null;
assert.match(qaCitation(ref),/Unknown citations/);
assert.match(qaCitation(ref),/<dt>Retraction status<\/dt><dd>Unknown/);
delete ref.paper.extra.paper_metadata;
assert.match(qaCitation(ref),/Paper metadata not recorded/);
''')
