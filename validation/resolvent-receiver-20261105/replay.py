"""Receive retained contributions in a disposable Resolvent at the pinned revision.

Offline: no model calls or research. A sealed synthetic workspace is retained as this version
records it, and as an earlier version recorded it (connector 4 round records, both artifacts
reference-packed, the earlier receiver pin). Each is exported over the HTTP API and received with
scripts/verify_resolvent_receiver.py. A negative control offers the first export to the earlier pin.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from canary import bundle_connectors, bundle_encoding, compact_bundle, dashboard, runs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.verify_resolvent_receiver import verify  # noqa: E402

EARLIER_PIN = '7d7e2da29b97814a70b42bd7bf442ee455b3da09'
KEPT = ('handoff_bytes', 'receiver_revision', 'recorded_contract_revision', 'research_artifact_read_by_receiver',
        'claims_submitted', 'retained_excerpts_verified', 'dry_run', 'apply', 'compiler_require_closure',
        'idempotent_retry')


def receiver(clone, revision, path):
    subprocess.run(['git', 'clone', '--quiet', '--no-checkout', str(clone), str(path)], check=True)
    subprocess.run(['git', 'checkout', '--quiet', revision], cwd=path, check=True)
    return path


def decisions(contribution, path):
    claims = [c['id'] for c in contribution['claims']]
    path.write_text(json.dumps({
        'claims': {c: {'action': 'accept', 'reason': 'Synthetic receiving proof; not a scientific review.'}
                   for c in claims},
        'connection_review': {c: {'considered_claim_ids': [], 'outcome': 'deferred',
                                  'reason': 'The disposable receiver holds no other claims.'} for c in claims},
        'follow_up_gaps': ['Synthetic receiving proof: relationships to other claims were not assessed.']}))
    return path


def retain(source, path, earlier):
    shutil.copytree(source, path)
    (path / '.run-reservation').touch()
    rounds = json.loads((path / 'payload.json').read_text())['claim_revisions']
    compact_bundle.retain(path)
    if earlier:  # The retained file an earlier version wrote for the same run.
        value = compact_bundle.read(path, expanded=True)
        value['method_metadata'].update(connector_version=4, claim_revisions=rounds,
                                        destination_contract_revision=EARLIER_PIN)
        compressed, bundle_encoding.COMPRESSED = bundle_encoding.COMPRESSED, None
        try:
            value = bundle_encoding.encode(value)
        finally:
            bundle_encoding.COMPRESSED = compressed
        body = (bundle_encoding.dumps(value) + '\n').encode()
        (path / compact_bundle.NAME).write_bytes(body)
        (path / compact_bundle.CHECKSUM).write_text(hashlib.sha256(body).hexdigest() + '\n')
    return (path / compact_bundle.NAME).stat().st_size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', help='A sealed, verified synthetic research workspace; it is copied, not changed')
    parser.add_argument('--receiver', required=True, help='A Resolvent clone containing both revisions')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='canary-receiver-'))
    server = dashboard.make_server(0, registry=root / 'registry.json', root=root)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api, cases = f'http://127.0.0.1:{server.server_port}', []
    for key, earlier in (('current', False), ('earlier-retained', True)):
        stored = retain(Path(args.workspace), root / key, earlier)
        runs.register_start(key, 'Synthetic receiving proof', '', kind='question', bundle=str(root / key),
                            status='done', path=server.RequestHandlerClass.supervisor.registry)
        started = time.process_time()
        contribution = compact_bundle.manifest(root / key)['contribution']
        cpu = time.process_time() - started
        exported = bundle_connectors.export(root / key, 'resolvent')
        report = verify(api, key, receiver(args.receiver, bundle_connectors.RESOLVENT_REVISION, root / (key + '-receiver')),
                        decisions(exported, root / (key + '-decisions.json')), root / (key + '-receipt'))
        cases.append({'case': key, 'stored_bytes': stored, 'contribution': contribution,
                      'manifest_cpu_ms': round(cpu * 1000), 'receipt': {k: report[k] for k in KEPT}})
    control = receiver(args.receiver, EARLIER_PIN, root / 'control-receiver')
    sent = next((root / 'current-receiver' / 'input-artefacts').glob('RUN-*.json'))
    shutil.copy(sent, control / 'input-artefacts' / sent.name)
    result = subprocess.run([sys.executable, 'scripts/world_control.py', str(control / 'input-artefacts' / sent.name),
                             '--decisions', str(root / 'current-decisions.json')], cwd=control, text=True,
                            capture_output=True)
    cases.append({'case': 'current export offered to the earlier pin', 'receiver_revision': EARLIER_PIN,
                  'handoff_bytes': sent.stat().st_size, 'returncode': result.returncode,
                  'output': ((result.stdout + result.stderr).strip().splitlines() or [''])[-1]})
    server.shutdown()
    print(json.dumps({'scope': 'Offline receiving proof on synthetic research; no model calls, no scientific '
                               'review and no canonical publication.',
                      'pinned_revision': bundle_connectors.RESOLVENT_REVISION, 'cases': cases}, indent=2))


if __name__ == '__main__':
    main()
