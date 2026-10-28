"""Retain a standalone Resolvent contribution; release its owned working files."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil

NAME = 'resolvent.json'
CHECKSUM = '.resolvent.sha256'
LIMITATION = ('Only the research bundle, exact cited excerpts and source locators/hashes are retained. '
              'Full papers and complete extracted texts are temporary working data; '
              'their bytes cannot be reconstructed from this bundle alone.')


def exists(workspace):
    return (Path(workspace) / NAME).is_file()


def read(workspace):
    workspace = Path(workspace)
    body = (workspace / NAME).read_bytes()
    if hashlib.sha256(body).hexdigest() != (workspace / CHECKSUM).read_text().strip():
        raise ValueError('Retained research bundle changed since completion')
    result = json.loads(body)
    metadata = result['method_metadata']
    if metadata.get('retention', {}).get('mode') != 'bundle_only':
        raise ValueError('Unsupported retained research bundle')
    return result


def payload(contribution):
    return json.loads(next(a['content'] for a in contribution['result']['artifacts']
                           if a['name'] == 'canary-research.json'))


def manifest(workspace):
    from .research_spec import verify_compiled
    result = read(workspace)
    value = json.loads(next(a['content'] for a in result['result']['artifacts']
                            if a['name'] == 'canary-bundle.json'))
    verify_compiled(value['specification'])
    value['artifacts'] = [{'path': NAME, 'bytes': (Path(workspace) / NAME).stat().st_size,
                          'sha256': (Path(workspace) / CHECKSUM).read_text().strip()}]
    value['retention'] = result['method_metadata']['retention']
    value['limitations'] = list(dict.fromkeys([*value['limitations'], LIMITATION]))
    return value


def projection(workspace):
    from .coverage_export import render_markdown
    contribution = read(workspace)
    saved = contribution['method_metadata']['canary_view']
    raw = payload(contribution)
    graph, run = saved['answer'], saved['run']
    report = render_markdown(graph)
    return dict(saved, kind='question', query_net=raw.get('question_plan', {}),
        coverage=graph.get('coverage', {}),
        paper_selection={'selection': raw.get('selection', {}), 'reviewed_papers': raw.get('reviewed_papers', [])},
        access={'accepted': [], 'excluded': [], 'scope_note': LIMITATION},
        seed=contribution['scope'], stopped=run.get('status'), model=run.get('model'),
        unanswered=graph.get('unanswered', []),
        synthesis_md=report + '\n' + LIMITATION + '\n' if report else None, synthesis_truncated=False,
        papers=[r['paper'] for r in graph.get('references', []) if isinstance(r.get('paper'), dict)],
        papers_total=run.get('discovered'), papers_truncated=False,
        iterations=[], assessments=[], decisions=[], retention=contribution['method_metadata']['retention'])


def record(workspace):
    return read(workspace)['method_metadata']['canary_record']


def retain(workspace):
    """Called only for a newly reserved API worker, after its final inventory seal.

    Commit and verify the complete output before removing any working file.
    Historical directories and the native CLI's account/session store are untouched.
    """
    from . import bundle_connectors, dashboard, runs
    workspace = Path(workspace)
    if exists(workspace):
        return manifest(workspace)
    if not (workspace / '.run-reservation').is_file():
        raise ValueError('Only a newly reserved research workspace can be released')
    contribution = bundle_connectors.resolvent(workspace)
    run = json.loads((workspace / 'run.json').read_text())
    view = dashboard.question_research_bundle(workspace, run)
    parsed = runs.parse_bundle(workspace)
    summary = dashboard.research_summary({'bundle': str(workspace)})
    # Keep the existing, verified graph so the frontend does not reinterpret research.
    saved = {key: copy.deepcopy(view.get(key)) for key in
             ('answer', 'run', 'peer_review', 'profile', 'usage', 'source_efficiency', 'startup_error')}
    for reference in saved['answer'].get('references', []):
        reference.get('paper', {}).get('extra', {}).pop('local_file', None)
        for alias in reference.get('paper', {}).get('extra', {}).get('source_aliases', []):
            alias.pop('local_file', None)
        reference['source_retention'] = 'excerpts_only'
    retention = {'mode': 'bundle_only', 'source_bytes_retained': False,
                 'verbatim_retained': True, 'limitation': LIMITATION}
    contribution['method_metadata'].update(retention=retention, canary_view=saved,
        canary_record=dict(parsed, log_files=[], retention=retention, research_summary=summary))
    contribution['result']['limitations'] = [line for line in contribution['result']['limitations']
        if not line.startswith('Preserve the accompanying neutral bundle archive')]
    contribution['result']['limitations'].append(LIMITATION)
    body = (json.dumps(contribution, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    temporary = workspace / (NAME + '.tmp')
    with temporary.open('wb') as stream:
        stream.write(body)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    (workspace / CHECKSUM).write_text(hashlib.sha256(body).hexdigest() + '\n')
    temporary.replace(workspace / NAME)
    read(workspace)  # No release on a failed output commit.
    for path in workspace.iterdir():
        if path.name in (NAME, CHECKSUM):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    return manifest(workspace)
