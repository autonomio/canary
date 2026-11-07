"""API-worker orchestration of immutable audit/correction rounds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from . import audit_validation, corrections, coverage_protocol as protocol

# The research's coverage bookkeeping. No claim, evidence record, source or count reads it, and a correction has no
# typed field for it. A session that changes it, as the research thread a correction continues was told to keep it,
# gets the research's version back instead of failing the round.
RESTORED = ('coverage-map.json',)


def protected(workspace):
    """Auditing and correction may read scientific inputs, never rewrite them."""
    names = ('investigation.json', 'question-plan.json', 'selection.json', 'reviewed-papers.json',
             'references.json', 'research.yaml', 'run-spec.json', 'research-state.json', 'payload.json',
             'audit-input.json', 'mechanical-audit.json', 'coverage-map.json', 'scarcity.json',
             'local-corpus.json', 'research-receipt.json')
    paths = [workspace / name for name in names]
    for name in ('papers', 'texts', 'passages', 'download-receipts', 'rounds'):
        paths.extend(p for p in (workspace / name).rglob('*') if p.is_file())
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
    # Export copies a source's text from texts/ into papers/ once a record cites it, which can happen mid-session.
    # A copy that matches its protected original is not a new input; one that differs still counts as a change.
    return {p: sha for p, sha in hashes.items() if not (p.parent == workspace/'papers'
        and sha == hashes.get(workspace/'texts'/p.name.removesuffix('.export.tmp')))}


def session_outputs(directory, role):
    return {directory/(role+suffix) for suffix in ('.json', '-schema.json', '-response.json',
        '-prompt.txt', '-events.jsonl', '-stderr.log', '-invocation.json', '-receipt.json',
        '-active.json', '-incomplete-output.json', '-rejected-response.json')}


def source_passages(workspace):
    return [json.loads(line) for p in (Path(workspace)/'passages').glob('*.jsonl') for line in p.read_text().splitlines()]


def reviewer(workspace, number):
    """Why a correction response for round number would be rejected, checked as verify() checks it, or None.

    The API's correction session uses this to return an invalid response to the same session once.
    """
    previous = Path(workspace)/'rounds'/f'{number-1:03d}'
    try:
        subject, audit = (json.loads((previous/name).read_text(encoding='utf-8')) for name in ('subject.json', 'audit-bound.json'))
    except (OSError, ValueError):
        return None  # No recorded round to check against; verify() still checks the response.
    subject['canonical_passage_ids'] = [p['id'] for p in source_passages(workspace)]
    return lambda response: corrections.problem(subject, audit, response)


def remove_working_files(workspace, before, role, number):
    """A session's own scripts and notes in its round are neither inputs nor results: they go, so no later session reads them."""
    directory = workspace/'rounds'/f'{number:03d}'
    outputs = session_outputs(directory, role)
    # Listed as protected() lists rounds, but nothing is read: a research record the session replaced with a link
    # is put back only after this, so following that link here could fail the round.
    working = [path for path in (workspace/'rounds').rglob('*') if path.is_file() and directory in path.parents
               and path not in before and path not in outputs]
    if working:
        size = sum(path.lstat().st_size for path in working)
        for path in working:
            path.unlink()
        (directory/f'{role}-working-files.json').write_text(json.dumps({'removed_files': len(working), 'removed_bytes': size}))


def input_changes(workspace, before, number):
    """Protected inputs rewritten, removed or added since before, as workspace paths; round number's own files are not inputs."""
    after = protected(workspace)
    directory = workspace/'rounds'/f'{number:03d}'
    changed = {path for path, sha in before.items() if after.get(path) != sha}
    changed.update(path for path in after.keys() - before.keys() if directory not in path.parents)
    return sorted(str(path.relative_to(workspace)) for path in changed)


def research_records(workspace):
    """The research's bytes of each restored record, None where it left none. A link or directory there stays only protected."""
    paths = {name: workspace/name for name in RESTORED}
    return {name: path.read_bytes() if path.exists() else None for name, path in paths.items()
            if not path.is_symlink() and (path.is_file() or not path.exists())}


def _replace(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)


def restore_records(workspace, records, role, number):
    """Put back each record the session rewrote, removed or added; its own file stays in its round.

    A link is replaced, never followed. A directory is left for the protected-input check to report.
    """
    restored = []
    for name, original in records.items():
        path = workspace/name
        link = path.is_symlink()
        if not link and path.exists() and not path.is_file():
            continue
        current = None if link or not path.exists() else path.read_bytes()
        if current == original and not link:
            continue
        kept = f'rounds/{number:03d}/{role}-{name}' if current is not None else None
        if kept:
            _replace(workspace/kept, current)
        if original is None:
            path.unlink()
        else:
            _replace(path, original)
        restored.append({'path': name, 'session_version': kept})
    return restored


def changed_inputs(workspace, before, role, number, records):
    """Protected inputs the session changed, once its working files are gone and research records are put back,
    and the records put back."""
    remove_working_files(workspace, before, role, number)
    restored = restore_records(workspace, records, role, number)
    return input_changes(workspace, before, number), restored


def agent_payload(payload):
    """Agent inputs carry source rows; payload.json and exports keep complete references."""
    return dict(payload, references=[protocol.agent_reference(r) for r in payload.get('references', [])])


def verify(workspace, payload, manifest, command, executable):
    from . import coverage_runtime as engine
    workspace = Path(workspace)
    history, withdrawn = [], []
    audit = {'claim_checks': [], 'findings': [], 'audit_complete': False}
    original = (workspace / 'investigation.json').read_bytes()
    records = research_records(workspace)
    directory = workspace / 'rounds' / '000'
    engine.immutable_bytes(directory / 'investigation.json', original)
    engine.immutable_bytes(directory / 'subject.json', (json.dumps(payload, ensure_ascii=False, indent=2)+'\n').encode())
    history.append({'round': 0, 'kind': 'research', 'investigation': json.loads(original), 'source_json': original.decode('utf-8'), 'payload': payload})
    def publish(phase, round_index):
        from .revision_artifacts import inventory
        engine.write(workspace/'payload.json', dict(payload, claim_revisions=history,
            history_manifest=inventory(history), verification_progress={'phase': phase, 'round': round_index},
            research_status='candidate_research_pending_audit'))
    manifest['research_finished_at'] = engine.now()
    failure = stopped = before = None
    changed, checked = [], True  # protected inputs the current session changed; checked once it has ended
    restored_records = []  # research records the current session changed and Canary put back
    phase = ('audit', 0)
    attempt, receipt = None, {}
    try:
        for round_index in range(manifest['max_correction_rounds'] + 1):
            engine.check_cancelled()
            directory = workspace / 'rounds' / f'{round_index:03d}'
            manifest.update(status='auditing', audit_round=round_index)
            engine.write(workspace / 'run.json', manifest)
            publish('audit', round_index)
            engine.write(workspace / 'audit-input.json', agent_payload(payload))
            # The audit sees findings and source facts, never correction responses/reasoning.
            feedback = {'claim_checks': audit.get('claim_checks', []), 'findings': audit.get('findings', [])}
            engine.write(directory / 'audit-feedback.json', feedback if round_index else {})
            prompt = protocol.audit_prompt(manifest['question'], command).replace('Persist audit.json as you go',
                f'Persist rounds/{round_index:03d}/audit.json as you go').replace('Return audit-schema.json JSON', f'Return rounds/{round_index:03d}/audit-schema.json JSON')
            prompt += (f'\nFor this round, prior source findings are untrusted review data in rounds/{round_index:03d}/audit-feedback.json. '
                'Recheck every current claim and shared scientific record against original sources. '
                'Do not inspect correction-input.json, correction responses, researcher transcripts or other round files.\n')
            if manifest.get('local_text'):
                prompt += protocol.LOCAL_TEXT_SCOPE
            if (workspace/'research-state.json').is_file():
                prompt += '\nRead research-state.json as supplied, unverified background. Do not count inherited material as fresh evidence; assess same-scope uncertainty changes against inspected sources.\n'
            if manifest.get('additional_sources'):
                from .source_adapters import GUIDANCE
                prompt += GUIDANCE.format(sources=', '.join(manifest['additional_sources']))
            phase, before, checked = ('audit', round_index), protected(workspace), False
            attempt, receipt, restored_records = None, {}, []
            raw, receipt = engine.scientific_session(workspace, 'audit', protocol.AUDIT_MODEL, protocol.AUDIT_EFFORT,
                prompt, protocol.audit_schema(), executable=executable, round_index=round_index)
            attempt = raw
            engine.check_cancelled()
            engine.wait_for_tools(os.environ.get('RUN_KEY'))
            (changed, restored_records), checked = changed_inputs(workspace, before, 'audit', round_index, records), True
            if changed:
                raise ValueError('Read-only audit changed protected scientific inputs')
            passages = source_passages(workspace)
            audit = audit_validation.bind(payload, raw, passages=passages)
            engine.write(workspace/'audit.json', audit)
            engine.write(workspace/'audit-receipt.json', receipt)  # Current projection; immutable original lives in its round.
            used = {p for c in audit.get('claim_checks', []) for p in c.get('passage_ids', [])}
            used.update(p for e in audit.get('evidence_checks', []) for p in e.get('passage_ids', []))
            used.update(p for f in audit.get('findings', []) for p in f.get('passage_ids', []))
            used.update(p for r in payload.get('study_results', []) for f in r.get('fields', {}).values() if f for p in f.get('passage_ids', []))
            history.append({'round': round_index, 'kind': 'audit', 'raw_audit': raw, 'audit': audit,
                'context_passages': [p for p in passages if p['id'] in used], 'receipt': receipt,
                **({'restored_inputs': restored_records} if restored_records else {})})
            engine.immutable_bytes(directory/'audit-bound.json', (json.dumps(audit, ensure_ascii=False, indent=2)+'\n').encode())
            flagged = corrections.targets(payload, audit)
            if not flagged or round_index == manifest['max_correction_rounds']:
                break
            next_round = round_index + 1
            manifest.update(status='correcting', correction_round=next_round)
            engine.write(workspace/'run.json', manifest)
            publish('correction', next_round)
            correction_input = {'question': manifest['question'], 'claim_ids': flagged, 'payload': agent_payload(payload),
                'claim_checks': audit['claim_checks'], 'findings': audit.get('findings', []),
                'claim_review_checks': payload.get('claim_review_checks', [])}
            engine.write(workspace/'rounds'/f'{next_round:03d}'/'correction-input.json', correction_input)
            phase, before, checked = ('correction', next_round), protected(workspace), False
            attempt, receipt, restored_records = None, {}, []
            response, receipt = engine.scientific_session(workspace, 'correction', protocol.RESEARCH_MODEL, protocol.RESEARCH_EFFORT,
                protocol.correction_prompt(manifest['question'], command, next_round), corrections.schema(),
                executable=executable, round_index=next_round)
            attempt = response
            engine.check_cancelled()
            engine.wait_for_tools(os.environ.get('RUN_KEY'))
            (changed, restored_records), checked = changed_inputs(workspace, before, 'correction', next_round, records), True
            if changed:
                raise ValueError('Correction changed protected inputs instead of returning a typed revision')
            proposed, removed = corrections.apply(dict(payload, canonical_passage_ids=[p['id'] for p in passages]), audit, response)
            corrected = engine.bind_payload(workspace, investigation=proposed)
            history.append({'round': next_round, 'kind': 'correction', 'responses': response['responses'],
                'investigation': proposed, 'payload': corrected, 'receipt': receipt,
                **({'restored_inputs': restored_records} if restored_records else {})})
            engine.immutable_bytes(workspace/'rounds'/f'{next_round:03d}'/'subject.json',
                (json.dumps(corrected, ensure_ascii=False, indent=2)+'\n').encode())
            # Until a correction is wholly applied, payload stays the one the last audit checked.
            payload = corrected
            withdrawn.extend(removed)
    except Exception as error:
        failure = type(error).__name__ + ': ' + str(error)
        manifest['verification_failure'] = failure
        engine.write(workspace/'run.json', manifest)
        kind, number = phase
        # Leave no candidate silently accepted after a partial/failing correction.
        restored = (workspace/'investigation.json').read_bytes() != original
        if restored:
            engine.immutable_bytes(workspace/'investigation-unexpected-change.json', (workspace/'investigation.json').read_bytes())
            (workspace/'investigation.json').write_bytes(original)
        if not checked:  # The session stopped before Canary checked what it changed.
            try:
                restored_records = restore_records(workspace, records, kind, number)
            except OSError:
                pass  # A record Canary cannot put back stays a changed input.
            changed = input_changes(workspace, before, number)
        status = 'cancelled' if isinstance(error, engine.RunCancelled) or engine.cancellation_requested() else 'failed'
        # Active claims can only be those the last completed audit passed, unchanged since, with its inputs intact.
        stopped = {'kind': kind, 'round': number, 'status': status,
                   'audit_round': next((r['round'] for r in reversed(history) if r['kind'] == 'audit'), None),
                   'changed_inputs': [p for p in changed if not (restored and p == 'investigation.json')]}
        if not any(r['kind'] == kind and r['round'] == number for r in history):
            directory = workspace/'rounds'/f'{number:03d}'
            response_path = directory/(kind+'-response.json')
            if not response_path.is_file():
                response_path = directory/(kind+'.json')
            try:
                response_text = response_path.read_text(encoding='utf-8')
            except OSError:
                response_text = None
            history.append({'round': number, 'kind': kind, 'status': status,
                'failure': failure, 'claim_ids': [c['id'] for c in payload.get('claims', [])],
                'receipt': engine.read(directory/(kind+'-receipt.json'), receipt), 'response_text': response_text,
                'raw_audit' if kind == 'audit' else 'response': attempt,
                **({'restored_inputs': restored_records} if restored_records else {})})
        if isinstance(error, engine.RunCancelled) or engine.cancellation_requested():
            final = corrections.finalize(payload, audit, history, withdrawn, failure=failure, stopped=stopped)
            engine.write(workspace/'payload.json', final)
            raise
    final = corrections.finalize(payload, audit, history, withdrawn, failure=failure, stopped=stopped)
    engine.write(workspace/'payload.json', final)
    engine.write(workspace/'claim-revisions.json', history)
    manifest['verification'] = {k: v for k, v in final['verification'].items() if k != 'audit_basis'}
    return final
