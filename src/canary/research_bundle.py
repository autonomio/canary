"""Versioned portable bundle storage; consumer-neutral, no destination writes."""
from __future__ import annotations

import hashlib
import json
import uuid
import sys
import datetime
import zipfile
from pathlib import Path
from typing import TypedDict

from .research_spec import verify_compiled, digest

BUNDLE_VERSION = 1
INPUT_FILES = ('research.yaml', 'run-spec.json', 'compile-report.json')
ARTIFACT_DIRS = {'papers', 'texts', 'passages', 'download-receipts', 'download-history', 'search-responses'}


class ResearchBundle(TypedDict):
    format: str
    version: int
    id: str
    created_at: str | None
    state: str
    specification: dict | None
    artifacts: list[dict]
    limitations: list[str]


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def initialize(workspace, packet):
    workspace = Path(workspace)
    packet = verify_compiled(packet)
    for name in INPUT_FILES:
        if (workspace / name).exists():
            raise ValueError('Research input already exists: ' + name)
    (workspace / 'research.yaml').write_text(packet['source_yaml'])
    write_json(workspace / 'run-spec.json', packet['resolved'])
    report = {k: v for k, v in packet.items() if k not in ('source_yaml', 'resolved')}
    report['bundle_id'] = 'RB-' + uuid.uuid4().hex
    report['created_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report['engine'] = {'python': sys.version.split()[0], 'sources': {
        name: file_hash(Path(__file__).parent / name) for name in
        ('coverage_protocol.py', 'coverage_runtime.py', 'coverage_export.py', 'claim_freshness.py',
         'research_spec.py', 'research_bundle.py', 'bundle_connectors.py')}}
    lock = Path(__file__).resolve().parents[2] / 'uv.lock'
    report['engine']['dependency_lock_sha256'] = file_hash(lock) if lock.is_file() else None
    write_json(workspace / 'compile-report.json', report)
    seal(workspace, state='running')


def _artifact_paths(workspace):
    for path in sorted(workspace.iterdir()):
        if path.is_symlink() or path.name.startswith('.') or path.name in {'bundle.json', 'auth.json', 'credentials.json', 'config.json'}:
            continue
        if path.is_file() and path.suffix in ('.json', '.jsonl', '.yaml', '.txt', '.md', '.log', '.py'):
            yield path
        elif path.is_dir() and path.name in ARTIFACT_DIRS:
            for child in sorted(path.rglob('*')):
                if child.is_file() and not child.is_symlink() and not any(p.startswith('.') for p in child.relative_to(workspace).parts):
                    yield child


def seal(workspace, *, state='finished') -> ResearchBundle:
    workspace = Path(workspace).resolve()
    report = json.loads((workspace / 'compile-report.json').read_text())
    artifacts = []
    for path in _artifact_paths(workspace):
        if not path.resolve().is_relative_to(workspace):
            continue
        artifacts.append({'path': path.relative_to(workspace).as_posix(), 'bytes': path.stat().st_size,
                          'sha256': file_hash(path)})
    spec = json.loads((workspace / 'run-spec.json').read_text())
    run = json.loads((workspace / 'run.json').read_text()) if (workspace / 'run.json').is_file() else {}
    result = {'format': 'canary.research-bundle', 'version': BUNDLE_VERSION,
              'id': report['bundle_id'], 'created_at': report['created_at'],
              'state': state, 'execution': {'status': run.get('status'), 'answered': run.get('answered'),
                  'audit_complete': run.get('audit_complete')}, 'specification': {'source_yaml': (workspace / 'research.yaml').read_text(),
                  'resolved': spec, 'spec_sha256': report['spec_sha256'], 'compiler_version': report['compiler_version'],
                  'checks': report['checks'], 'engine': report['engine']},
              'artifacts': artifacts,
              'limitations': ['Compiled settings do not certify scientific interpretation, source completeness or search recall.',
                             'The saved specification repeats settings; live models, discovery and engine versions can change.']}
    write_json(workspace / 'bundle.json', result)
    return result


def read(workspace) -> ResearchBundle:
    path = Path(workspace) / 'bundle.json' if workspace else None
    if path is None or not path.is_file():
        return {'format': 'canary.research-bundle', 'version': BUNDLE_VERSION, 'id': '', 'created_at': None, 'execution': {'status': None, 'answered': None, 'audit_complete': None}, 'state': 'legacy_unversioned',
                'specification': None, 'artifacts': [],
                'limitations': ['No compiled bundle manifest was recorded for this run. Existing research remains available.']}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Bundle manifest must be an object')
    if value.get('format') != 'canary.research-bundle' or value.get('version') != BUNDLE_VERSION:
        raise ValueError('Unsupported research bundle version')
    records = value.get('artifacts')
    if (not isinstance(records, list) or any(not isinstance(r, dict) or not isinstance(r.get('path'), str)
            or type(r.get('bytes')) is not int or r['bytes'] < 0 or not isinstance(r.get('sha256'), str)
            or len(r['sha256']) != 64 for r in records)
            or len({r['path'] for r in records}) != len(records)):
        raise ValueError('Malformed bundle artifact inventory')
    spec = value.get('specification')
    if not isinstance(spec, dict) or not isinstance(spec.get('resolved'), dict) or not isinstance(spec.get('source_yaml'), str) or not isinstance(spec.get('spec_sha256'), str):
        raise ValueError('Malformed bundle specification')
    if spec:
        records = {r['path']: r for r in value['artifacts']}
        for name in INPUT_FILES:
            target = Path(workspace) / name
            if name not in records or not target.is_file() or file_hash(target) != records[name]['sha256']:
                raise ValueError('Compiled bundle input changed: ' + name)
        actual = json.loads((Path(workspace) / 'run-spec.json').read_text())
        if (digest(actual) != spec['spec_sha256'] or actual != spec['resolved'] or
                (Path(workspace) / 'research.yaml').read_text() != spec['source_yaml']):
            raise ValueError('Bundle specification disagrees with the saved inputs')
    return value


def archive(workspace, target):
    workspace = Path(workspace)
    manifest = read(workspace)
    if manifest['state'] != 'finished':
        raise ValueError('Only a finished versioned bundle can be archived')
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as output:
        output.write(workspace / 'bundle.json', 'bundle.json')
        for entry in manifest['artifacts']:
            output.write(artifact(workspace, entry['path']), entry['path'])


def artifact(workspace, name):
    workspace = Path(workspace).resolve()
    records = {r['path']: r for r in read(workspace)['artifacts']}
    path = (workspace / name).resolve()
    if name not in records or not path.is_relative_to(workspace) or not path.is_file():
        raise KeyError('Unknown bundle artifact')
    if file_hash(path) != records[name]['sha256']:
        raise ValueError('Bundle artifact changed since its recorded snapshot')
    return path


def run_compiled(staged, workspace):
    from . import coverage_runtime, coverage_export
    packet = verify_compiled(json.loads(Path(staged).read_text()))
    spec = packet['resolved']
    workspace = Path(workspace)
    reserved = False
    def begin():
        nonlocal reserved
        initialize(workspace, packet)
        reserved = True
    try:
        return coverage_runtime.run(spec['question'], workspace,
            minimum_papers=spec['retrieval']['target_papers'], audit_seconds=spec['audit']['budget_seconds'],
            on_reserved=begin)
    finally:
        if reserved:
            try:
                coverage_export.export_bundle(workspace)
            finally:
                seal(workspace)


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 3:
        raise SystemExit('Usage: python -m canary.research_bundle STAGED_SPEC WORKSPACE')
    run_compiled(sys.argv[1], sys.argv[2])
