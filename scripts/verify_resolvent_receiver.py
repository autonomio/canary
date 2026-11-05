"""Prove API export -> disposable Resolvent admission. No publication or inference."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import httpx
import yaml


def sha(value):
    return hashlib.sha256(value).hexdigest()


def verify_archive(manifest, content, handoff):
    """Verify transport; reconstruct whole-text slices only when bytes are retained."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), 'Duplicate archive paths'
        for artifact in manifest['artifacts']:
            body = archive.read(artifact['path'])
            assert len(body) == artifact['bytes'] and sha(body) == artifact['sha256'], artifact['path']
        assert handoff['method_metadata']['bundle_id'] == manifest['id']
        if manifest.get('retention', {}).get('mode') == 'bundle_only':
            from canary import bundle_encoding, compact_bundle
            stored = bundle_encoding.expand(json.loads(archive.read(compact_bundle.NAME)))
            assert stored == bundle_encoding.expand(handoff), 'Archive and API contributions differ'
            payload = compact_bundle.payload(stored)
            refs = {r['id']: r for r in payload.get('references', [])}
            passages = {p['id']: p for e in payload.get('evidence', []) for p in e.get('verbatims', [])}
            source_map = stored['method_metadata']['id_map']['passages']
            sources = {s['id']: s for s in stored['sources']}
            for pid, sid in source_map.items():
                passage, source = passages[pid], sources[sid]
                locator = json.loads(source['locator'])
                reference = refs[locator['reference_id']]
                assert source['excerpt'] == passage['text'], sid
                assert all(locator[k] == passage[k] for k in ('start', 'end')), sid
                assert locator['original_sha256'] == reference['body_sha256'], sid
                assert locator['text_sha256'] == reference['text_sha256'], sid
            # This checks retained exact excerpts and provenance, not absent full originals.
            return len(sources)
        originals = {a['path']: a for a in manifest['artifacts']}
        for source in handoff['sources']:
            locator = json.loads(source['locator'])
            assert locator['bundle_id'] == manifest['id']
            text = archive.read(locator['text_file']).decode('utf-8')
            assert text[locator['start']:locator['end']] == source['excerpt'], source['id']
            for name, digest in (('text_file', 'text_sha256'), ('original_file', 'original_sha256')):
                assert originals[locator[name]]['sha256'] == locator[digest], source['id']
    return len(handoff['sources'])


def verify(api_url, run, receiver, decisions, output):
    receiver, output = Path(receiver).resolve(), Path(output).resolve()
    # This verifier is deliberately for an empty disposable receiver only.
    assert not list((receiver/'input-artefacts').glob('RUN-*.json')), 'Receiver already contains contributions'
    assert not list((receiver/'claims').glob('C-*.yaml')), 'Receiver already contains claims'
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=receiver, text=True).strip()
    with httpx.Client(base_url=api_url, timeout=120, trust_env=False) as api:
        base = '/api/runs/' + run
        def get(path):
            response = api.get(base + path)
            response.raise_for_status()
            return response
        manifest = get('/bundle').json()['bundle']
        assert manifest['state'] == 'finished', 'Research must have a sealed inventory'
        exported = get('/exports/resolvent')
        handoff = exported.json()
        archive = get('/bundle/archive').content
    assert revision == handoff['method_metadata']['destination_contract_revision'], 'Use the recorded receiver revision'
    excerpts = verify_archive(manifest, archive, handoff)
    output.mkdir(parents=True, exist_ok=False)
    (output/'research-bundle.zip').write_bytes(archive)
    incoming = receiver/'input-artefacts'/(handoff['id']+'.json')
    # The exact size-checked export; re-indenting it could exceed the receiver's file limit.
    incoming.write_bytes(exported.content + b'\n')
    (output/'admission-decisions.json').write_bytes(Path(decisions).read_bytes())
    command = [sys.executable, 'scripts/world_control.py', str(incoming), '--decisions',
               str(output/'admission-decisions.json')]
    results = []
    for arguments in (command, command+['--apply'], [sys.executable, 'scripts/compiler.py', '--require-closure'],
                      command+['--apply']):
        result = subprocess.run(arguments, cwd=receiver, text=True, capture_output=True, check=True)
        results.append({'command': arguments[1:], 'stdout': result.stdout, 'stderr': result.stderr})
    retry = json.loads(results[-1]['stdout'])
    assert retry['already_processed'] and retry['files'] == [], retry
    (output/'consumer-checks.json').write_text(json.dumps(results, indent=2)+'\n')
    closure = yaml.safe_load((receiver/'briefs/closed'/(handoff['id'].replace('RUN-', 'BC-')+'.yaml')).read_text())
    candidates = {c['id']:c for c in handoff['claims']}
    for disposition in closure['claim_dispositions']:
        if disposition['disposition'] == 'created':
            candidate = candidates[disposition['candidate_id']]
            canonical = yaml.safe_load((receiver/'claims'/(disposition['claim_id']+'.yaml')).read_text())
            for field in ('statement', 'class', 'uncertainty'):
                assert canonical[field] == candidate[field], (candidate['id'],field)
    canonical_sources = [yaml.safe_load(p.read_text()) for p in (receiver/'sources').glob('S-*.yaml')]
    for source in handoff['sources']:
        assert any(s['excerpt'] == source['excerpt'] and s['locator'] == source['locator']
                   and s['kind'] == source['kind'] and s['uri'] == source['uri']
                   for s in canonical_sources), source['id']
    compact = manifest.get('retention', {}).get('mode') == 'bundle_only'
    source_kinds = {}
    for source in canonical_sources:
        kind = json.loads(source['locator']).get('source_kind', source['kind'])
        source_kinds[kind] = source_kinds.get(kind, 0) + 1
    report = {'run_key': run, 'bundle_id': manifest['id'], 'receiver_revision': revision,
              'bundle_archive_sha256': sha(archive), 'handoff_sha256': sha(incoming.read_bytes()),
              'claims_submitted': len(handoff['claims']), 'verbatims_reconstructed': 0 if compact else excerpts,
              'retained_excerpts_verified': excerpts if compact else 0,
              'source_verification_scope': 'retained excerpts and locators; full originals released' if compact else 'transported originals and exact canonical slices',
              'canonical_source_kinds': source_kinds,
              'dry_run': True, 'apply': True, 'compiler_require_closure': True, 'idempotent_retry': True,
              'dispositions': closure['claim_dispositions'],
              'semantic_review': 'Not performed by this verifier; decisions are supplied separately.',
              'canonical_publication': False, 'disposable_receiver': True}
    (output/'receipt.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='http://127.0.0.1:8789')
    parser.add_argument('--run', required=True)
    parser.add_argument('--receiver', required=True)
    parser.add_argument('--decisions', required=True, help='Explicit admission-agent decisions; never generated by this verifier')
    parser.add_argument('--output', required=True, help='New directory for originals and operational receipts')
    args = parser.parse_args()
    print(json.dumps(verify(args.api, args.run, args.receiver, args.decisions, args.output), indent=2))


if __name__ == '__main__':
    main()
