"""Service-owned index of configured local research; source files stay untouched."""
import hashlib
import json
import os
from pathlib import Path
import re
import time

from . import runs

SKIP = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'papers', 'texts', 'passages'}


def roots(root):
    paths = [Path(root)/'out', Path(root)/'container-out']
    paths.extend(Path(p).expanduser() for p in os.environ.get('CANARY_RESEARCH_ROOTS', '').split(os.pathsep) if p)
    return list(dict.fromkeys(p.resolve() for p in paths))


def status(root, registry):
    rows = runs.load(registry)
    return {'roots': [{'path':str(p), 'exists':p.is_dir()} for p in roots(root)],
            'registered_runs':len(rows),
            'missing_bundles':sum(bool(r.get('bundle')) and not Path(r['bundle']).is_dir() for r in rows),
            'last_scan':runs._load_json(Path(registry).with_name('footprint.json'), None),
            'scope':'Configured server-side roots and registered runs. No whole-computer scan. Missing files and unknown activity remain explicit.'}


def refresh(root, registry):
    started = runs.utcnow()
    before = time.monotonic()
    found, errors = set(), []
    def error(exc):
        errors.append({'path':str(exc.filename or ''), 'error':str(exc)})
    for configured in roots(root):
        if not configured.exists():
            continue
        for directory, dirs, files in os.walk(configured, followlinks=False, onerror=error):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not Path(directory,d).is_symlink())
            path = Path(directory)
            native = 'run.json' in files
            bundle = runs._load_json(path/'bundle.json', {}) if 'bundle.json' in files else {}
            manifest = runs._load_json(path/'manifest.json', {}) if 'manifest.json' in files else {}
            exported = isinstance(manifest,dict) and 'status' in manifest and ('run_id' in manifest or 'spec' in manifest)
            if native or exported or isinstance(bundle,dict) and bundle.get('format')=='canary.research-bundle':
                found.add(path.resolve())
                dirs[:] = []  # Inner paper/analysis artifacts belong to this run.
    added = []
    with runs._registry_lock(registry):
        rows = runs.load(registry)
        known = {str(Path(r['bundle']).resolve()) for r in rows if r.get('bundle')}
        keys = {r.get('key') for r in rows}
        for path in sorted(found):
            if str(path) in known:
                continue
            try:
                parsed = runs.parse_bundle(path)
                key = 'local-'+re.sub(r'[^a-z0-9]+','-',path.name.lower()).strip('-')[:40]+'-'+hashlib.sha256(str(path).encode()).hexdigest()[:12]
                if key in keys:
                    raise ValueError('Local run key collision')
                rows.append(dict(parsed,key=key,name=path.name,brief='',launch='',container=parsed.get('container'),
                                 discovered_at=started))
                keys.add(key);known.add(str(path));added.append(key)
            except (OSError,ValueError,TypeError) as exc:
                errors.append({'path':str(path),'error':str(exc)})
        if added:
            rows.sort(key=lambda r:str(r.get('started_at','')))
            runs._save_locked(rows,registry)
    receipt = {'started_at':started,'finished_at':runs.utcnow(),'seconds':round(time.monotonic()-before,3),
               'added_keys':added,'errors':errors}
    target = Path(registry).with_name('footprint.json')
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(receipt,indent=2)+'\n')
    temporary.replace(target)
    return status(root,registry)
