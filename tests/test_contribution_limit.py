"""A verified run's Resolvent contribution fits the destination file limit, or Canary refuses it explicitly."""
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess

import jsonschema
import pytest

from canary import (api_contract, audit_validation, bundle_connectors, bundle_encoding, cli, compact_bundle,
                    corrections, coverage_export, coverage_runtime as engine, research_bundle, runs)
from canary.paper_metadata import snapshot
from test_bundle_encoding import earlier_layout
from test_corrections import audit_for, prepared, response
from test_coverage_evidence_ui import run_dashboard
from test_research_bundle import api  # noqa: F401
from test_verified_exports import finish

WORDS = ('cohort measured outcome baseline treatment control interval variance trial dose response effect '
         'estimate population protocol analysis observed reported adjusted sample').split()


def provider_record(rng, identifier):
    """An OpenAlex-shaped discovery record; synthetic values only."""
    def text(n):
        return ' '.join(rng.choice(WORDS) for _ in range(n))
    def location():
        return {'is_oa': True, 'landing_page_url': f'https://example.org/{identifier}', 'version': 'publishedVersion',
                'source': {'id': f'https://openalex.org/S{rng.randrange(10**9)}', 'display_name': text(5).title(),
                           'issn': ['1234-5678'], 'host_organization_name': text(3).title(), 'type': 'journal'}}
    def topic():
        return {'id': f'https://openalex.org/T{rng.randrange(10**5)}', 'display_name': text(4), 'score': round(rng.random(), 4),
                'subfield': {'display_name': text(2)}, 'field': {'display_name': text(2)}, 'domain': {'display_name': text(2)}}
    return {'id': f'https://openalex.org/{identifier}', 'display_name': text(12).capitalize(), 'doi': None,
            'publication_year': 2020, 'publication_date': '2020-05-17', 'cited_by_count': rng.randrange(300),
            'counts_by_year': [{'year': 2025 - y, 'cited_by_count': rng.randrange(40)} for y in range(8)],
            'fwci': round(rng.random() * 3, 3), 'citation_normalized_percentile': {'value': round(rng.random(), 6),
                'is_in_top_1_percent': False, 'is_in_top_10_percent': False},
            'authorships': [{'author_position': 'middle', 'author': {'id': f'https://openalex.org/A{rng.randrange(10**9)}',
                'display_name': text(2).title()}, 'institutions': [{'id': f'https://openalex.org/I{rng.randrange(10**8)}',
                'display_name': text(4).title(), 'country_code': 'FI', 'type': 'education'}],
                'raw_affiliation_strings': [text(14)]} for _ in range(8)],
            'primary_location': location(), 'best_oa_location': location(), 'locations': [location(), location()],
            'open_access': {'is_oa': True, 'oa_status': 'gold'}, 'type': 'article', 'language': 'en',
            'primary_topic': topic(), 'topics': [topic() for _ in range(3)],
            'keywords': [{'display_name': text(2), 'score': round(rng.random(), 6)} for _ in range(5)],
            'is_retracted': False, 'referenced_works': [f'https://openalex.org/W{rng.randrange(10**10)}' for _ in range(45)],
            'referenced_works_count': 45, 'updated_date': '2026-10-30T05:12:44.123456'}


def corrected_run(workspace, *, retrieved=4, assessed=24, rounds=3):
    """A verified run whose three correction rounds each bound the whole research graph, assessed candidates included."""
    prepared(workspace, rounds)
    rng = random.Random(185)
    engine.write(workspace / 'selection.json', {'selected_for_review_ids': ['W1'], 'decisions': [
        {'work_id': f'W{5000 + n}', 'decision': 'exclude', 'evidence_role': 'unknown', 'priority_rank': n + 1,
         'reason': ' '.join(rng.choice(WORDS) for _ in range(30)),
         'supporting_abstract_text': ' '.join(rng.choice(WORDS) for _ in range(300))} for n in range(assessed)]})
    for n in range(retrieved):
        identifier = f'W{100 + n}'
        text = ' '.join(rng.choice(WORDS) for _ in range(120))
        for folder in ('papers', 'texts'):
            (workspace / folder / (identifier + '.txt')).write_text(text)
        sha = hashlib.sha256(text.encode()).hexdigest()
        engine.write(workspace / 'download-receipts' / (identifier + '.json'), {'reference': {
            'id': identifier, 'title': f'Synthetic retrieved source {n}', 'url': f'https://example.org/{identifier}',
            'file': f'papers/{identifier}.txt', 'text_file': f'texts/{identifier}.txt', 'body_sha256': sha, 'text_sha256': sha,
            'paper_metadata': snapshot(provider_record(rng, identifier), '2026-10-30T05:00:00+00:00')}})
    raw = engine.read(workspace / 'investigation.json')
    raw['claims'][0].update(kind='reported_finding', study_result_ids=['S1'])
    raw['evidence'][0]['relation'] = 'supports'
    raw['study_results'] = [{'id': 'S1', 'reference_ids': ['W1'], 'result_type': 'measured',
        'fields': {name: ({'text': 'Synthetic value', 'passage_ids': ['W1-p0001']} if name == 'value' else None)
            for name in ('system', 'intervention', 'comparator', 'conditions', 'endpoint', 'value', 'units', 'sample_size',
                         'reported_uncertainty')}, 'version_or_cohort_note': 'Synthetic cohort only.'}]
    engine.write(workspace / 'investigation.json', raw)
    original = (workspace / 'investigation.json').read_text()
    payload = engine.bind_payload(workspace)
    assert payload['mechanical_audit']['pass'], payload['mechanical_audit']['issues']
    history = [{'round': 0, 'kind': 'research', 'investigation': raw, 'source_json': original, 'payload': payload}]
    for number in range(rounds):
        review = audit_for(payload, 'correction_needed')
        flagged = audit_validation.bind(payload, review)
        history.append({'round': number, 'kind': 'audit', 'raw_audit': review, 'audit': flagged})
        revision = response(payload)
        revision['investigation']['claims'][0]['text'] = f'Synthetic attributed finding, revised in round {number + 1}.'
        proposed, _ = corrections.apply(payload, flagged, revision)
        payload = engine.bind_payload(workspace, investigation=proposed)
        history.append({'round': number + 1, 'kind': 'correction', 'responses': revision['responses'],
                        'investigation': proposed, 'payload': payload})
    review = audit_for(payload)
    bound = audit_validation.bind(payload, review)
    history.append({'round': rounds, 'kind': 'audit', 'raw_audit': review, 'audit': bound})
    final = corrections.finalize(payload, bound, history)
    assert [c['id'] for c in final['claims']] == ['C1']
    engine.write(workspace / 'payload.json', final)
    engine.write(workspace / 'audit.json', bound)
    run = engine.read(workspace / 'run.json')
    run.update(status='complete', answered=True)
    engine.write(workspace / 'run.json', run)
    coverage_export.export_bundle(workspace)
    research_bundle.seal(workspace)
    return final


def test_verified_run_that_exceeded_the_limit_now_fits_with_record_audit_and_claims_intact(tmp_path, monkeypatch):
    # The run and the 10 MiB destination limit are scaled down together, so the suite keeps
    # its CI time budget; both layouts are compared at the same scale.
    scaled = 200_000
    monkeypatch.setattr(bundle_connectors, 'CONTRIBUTION_LIMIT_BYTES', scaled)
    workspace = tmp_path / 'large'
    workspace.mkdir()
    final = corrected_run(workspace)
    native = bundle_connectors.resolvent(workspace)
    (workspace / '.run-reservation').touch()
    manifest = compact_bundle.retain(workspace)
    compact = compact_bundle.read(workspace)  # The retained file is the compact export.
    stored = (workspace / compact_bundle.NAME).stat().st_size
    assert manifest['contribution'] == bundle_connectors.contribution(compact) == {
        'bytes': stored, 'limit_bytes': scaled, 'within_limit': True}
    assert manifest['encoding']['version'] == bundle_encoding.VERSION
    expanded = bundle_encoding.expand(compact)
    # Before: version 4 listed complete round records natively and the bundle manifest was reference-packed.
    previous = json.loads(json.dumps(expanded))
    previous['method_metadata'].update(connector_version=4, claim_revisions=final['claim_revisions'])
    assert bundle_connectors.contribution(earlier_layout(previous))['bytes'] > scaled
    schema = json.loads((Path(__file__).parent / 'fixtures/resolvent-submission.schema.json').read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(compact)
    # The receiver's research-state reader decodes the listed version 1 artifact; the manifest is compressed.
    header = compact['method_metadata']['canary_encoding']
    assert header['version'] == 1 and [a['name'] for a in header['artifacts']] == ['canary-research.json']
    assert compact['method_metadata']['destination_contract_revision'] == bundle_connectors.RESOLVENT_REVISION
    jsonschema.validate(manifest, {'$ref': '#/components/schemas/BundleManifest',
                                   'components': {'schemas': api_contract.schemas()}})
    for field in ('sources', 'evidence', 'claims', 'connectors'):
        assert compact[field] == native[field]
    metadata = compact['method_metadata']
    for field in ('verification', 'independent_audit', 'history_manifest', 'claim_metadata', 'claim_revisions'):
        assert metadata[field] == native['method_metadata'][field]
    assert metadata['verification'] == {k: v for k, v in final['verification'].items() if k != 'audit_basis'}
    assert metadata['independent_audit'] == final['independent_audit']
    assert [c['statement'] for c in compact['claims']] == [c['text'] for c in final['claims']]
    assert compact['claims'][0]['statement'] == 'Synthetic attributed finding, revised in round 3.'
    assert [r.get('payload_sha256') is not None for r in metadata['claim_revisions']] == [
        'payload' in r for r in final['claim_revisions']]
    assert compact_bundle.payload(expanded) == final  # The research artifact keeps every complete round record.
    assert bundle_connectors.export(workspace, 'resolvent') == compact
    correction = research_bundle.revision(workspace, 3, 'correction')
    assert json.loads(correction['content'])['payload'] == final['claim_revisions'][-2]['payload']


def test_retained_contribution_refused_before_is_packed_again_within_the_limit(tmp_path, monkeypatch):
    workspace = tmp_path / 'retained'
    workspace.mkdir()
    final, _ = finish(workspace)
    (workspace / '.run-reservation').touch()
    compact_bundle.retain(workspace)
    # As an earlier Canary retained it: complete round records natively, both artifacts
    # reference-packed, and recorded against the earlier receiver pin.
    earlier = compact_bundle.read(workspace, expanded=True)
    earlier['method_metadata'].update(connector_version=4, claim_revisions=final['claim_revisions'],
                                      destination_contract_revision='7d7e2da29b97814a70b42bd7bf442ee455b3da09')
    earlier = earlier_layout(earlier)
    body = (bundle_encoding.dumps(earlier) + '\n').encode()
    (workspace / compact_bundle.NAME).write_bytes(body)
    (workspace / compact_bundle.CHECKSUM).write_text(hashlib.sha256(body).hexdigest() + '\n')
    assert bundle_connectors.export(workspace, 'resolvent') == earlier  # Within the limit: as recorded.
    again = bundle_encoding.encode(bundle_encoding.expand(earlier))
    size = bundle_connectors.contribution(again)['bytes']
    limit = (size + len(body)) // 2  # The recorded file is over it; packed again, it fits.
    monkeypatch.setattr(bundle_connectors, 'CONTRIBUTION_LIMIT_BYTES', limit)
    exported = bundle_connectors.export(workspace, 'resolvent')
    assert exported == again and bundle_encoding.header(exported)['compressed']
    assert compact_bundle.manifest(workspace)['contribution'] == {'bytes': size, 'limit_bytes': limit, 'within_limit': True}
    for field in ('sources', 'evidence', 'claims', 'connectors'):
        assert exported[field] == earlier[field]
    for field in ('claim_revisions', 'verification', 'independent_audit', 'destination_contract_revision'):
        assert exported['method_metadata'][field] == earlier['method_metadata'][field]
    assert bundle_encoding.expand(exported) == bundle_encoding.expand(earlier)
    assert (workspace / compact_bundle.NAME).read_bytes() == body  # The retained file is not rewritten.


def test_contribution_that_cannot_fit_is_refused_and_the_run_says_so(api, tmp_path, monkeypatch, capsys):
    server, client, _ = api
    workspace = tmp_path / 'refused'
    workspace.mkdir()
    finish(workspace)
    (workspace / '.run-reservation').touch()
    compact_bundle.retain(workspace)
    runs.register_start('refused', 'Fixture', '', kind='question', bundle=str(workspace), status='done',
                        path=server.RequestHandlerClass.supervisor.registry)
    size = (workspace / compact_bundle.NAME).stat().st_size
    monkeypatch.setattr(bundle_connectors, 'CONTRIBUTION_LIMIT_BYTES', size - 1)
    refused = client.get('/api/runs/refused/exports/resolvent')
    assert refused.status_code == 422
    body = refused.json()
    assert body['code'] == 'contribution_too_large' and body['bytes'] == size and body['limit_bytes'] == size - 1
    assert 'Nothing was exported' in body['error']
    contract = client.get('/openapi.json').json()
    schema = contract['paths']['/api/runs/{key}/exports/{connector}']['get']['responses']['422']['content']['application/json']['schema']
    jsonschema.validate(body, dict(schema, components=contract['components']))
    bundle = client.get('/api/runs/refused/bundle').json()['bundle']
    assert bundle['contribution'] == {'bytes': size, 'limit_bytes': size - 1, 'within_limit': False}
    assert client.get('/api/runs/refused/exports/resolvent?representation=expanded').status_code == 200
    assert client.get('/api/runs/refused/bundle/archive').status_code == 200
    assert cli.main(['bundle', 'refused', '--connector', 'resolvent', '--port', str(server.server_port)]) == 1
    assert 'Nothing was exported' in capsys.readouterr().err
    view = compact_bundle.projection(workspace)
    view['compiled_bundle'] = bundle
    run_dashboard(tmp_path, view['answer'], """
S.selected='refused';
const html=bundleHtml(""" + json.dumps(view) + """);
assert.match(html,/data-qa="contribution-too-large"/);
assert.match(html,/Canary does not export it/);
assert.match(html,/href="\\/api\\/runs\\/refused\\/bundle\\/archive"/);
assert.match(html,/Download retained bundle archive/);
assert.doesNotMatch(html,/Download research bundle/);
const fits=Object.assign({}, """ + json.dumps(view) + """, {compiled_bundle:Object.assign({}, """ + json.dumps(bundle) + """,
  {contribution:{bytes:4210000,limit_bytes:10485760,within_limit:true}})});
assert.match(bundleHtml(fits),/Resolvent contribution · 4.21 MB of the 10.49 MB \\(10 MiB\\) file limit/);
assert.match(bundleHtml(fits),/Download research bundle/);
""")


RECEIVER = os.environ.get('CANARY_RESOLVENT_RECEIVER')


@pytest.mark.skipif(not RECEIVER, reason='Set CANARY_RESOLVENT_RECEIVER to a Resolvent clone containing the pinned revision')
def test_pinned_receiver_admits_a_contribution_between_2_and_10_mib(api, tmp_path):
    """The actual receiving path: research reader, dry run, apply, closure compiler and idempotent retry."""
    from scripts.verify_resolvent_receiver import verify
    server, _, _ = api
    workspace = tmp_path / 'receiving'
    workspace.mkdir()
    corrected_run(workspace, assessed=1000, rounds=1)  # Its research artifact stays within the reader's 32 MiB.
    (workspace / '.run-reservation').touch()
    size = compact_bundle.retain(workspace)['contribution']['bytes']
    assert 2 * 1024 * 1024 < size <= bundle_connectors.CONTRIBUTION_LIMIT_BYTES == 10 * 1024 * 1024
    runs.register_start('receiving', 'Fixture', '', kind='question', bundle=str(workspace), status='done',
                        path=server.RequestHandlerClass.supervisor.registry)
    receiver = tmp_path / 'receiver'
    subprocess.run(['git', 'clone', '--quiet', '--no-checkout', RECEIVER, str(receiver)], check=True)
    subprocess.run(['git', 'checkout', '--quiet', bundle_connectors.RESOLVENT_REVISION], cwd=receiver, check=True)
    claims = [c['id'] for c in compact_bundle.read(workspace)['claims']]
    decisions = tmp_path / 'decisions.json'
    decisions.write_text(json.dumps({
        'claims': {c: {'action': 'accept', 'reason': 'Synthetic receiving proof; not a scientific review.'} for c in claims},
        'connection_review': {c: {'considered_claim_ids': [], 'outcome': 'deferred',
                                  'reason': 'The disposable receiver holds no other claims.'} for c in claims},
        'follow_up_gaps': ['Synthetic receiving proof: relationships to other claims were not assessed.']}))
    report = verify(f'http://127.0.0.1:{server.server_port}', 'receiving', receiver, decisions, tmp_path / 'receipt')
    assert report['receiver_revision'] == report['recorded_contract_revision'] == bundle_connectors.RESOLVENT_REVISION
    assert report['handoff_bytes'] == size
    assert report['research_artifact_read_by_receiver']['readable'] is True
    assert [d['disposition'] for d in report['dispositions']] == ['created'] * len(claims)
