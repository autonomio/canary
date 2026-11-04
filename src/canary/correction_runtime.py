"""API-worker orchestration of immutable audit/correction rounds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import audit_validation, corrections, coverage_protocol as protocol


def protected(workspace):
    """Auditing and correction may read scientific inputs, never rewrite them."""
    names = ('investigation.json', 'question-plan.json', 'selection.json', 'reviewed-papers.json',
             'references.json', 'research.yaml', 'run-spec.json', 'research-state.json', 'payload.json',
             'audit-input.json', 'mechanical-audit.json', 'coverage-map.json', 'scarcity.json',
             'local-corpus.json', 'research-receipt.json')
    paths = [workspace / name for name in names]
    for name in ('papers', 'texts', 'passages', 'download-receipts', 'rounds'):
        paths.extend(p for p in (workspace / name).rglob('*') if p.is_file())
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


def changed_inputs(workspace, before, role, number):
    after = protected(workspace)
    directory = workspace/'rounds'/f'{number:03d}'
    allowed = {directory/(role+suffix) for suffix in ('.json', '-schema.json', '-response.json',
        '-prompt.txt', '-events.jsonl', '-stderr.log', '-invocation.json', '-receipt.json',
        '-active.json', '-incomplete-output.json')}
    return any(after.get(path) != sha for path, sha in before.items()) or bool(after.keys() - before.keys() - allowed)


def verify(workspace, payload, manifest, command, executable):
    from . import coverage_runtime as engine
    workspace = Path(workspace)
    history, withdrawn = [], []
    audit = {'claim_checks': [], 'findings': [], 'audit_complete': False}
    original = (workspace / 'investigation.json').read_bytes()
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
    failure = None
    phase = ('audit', 0)
    attempt, receipt = None, {}
    try:
        for round_index in range(manifest['max_correction_rounds'] + 1):
            engine.check_cancelled()
            directory = workspace / 'rounds' / f'{round_index:03d}'
            manifest.update(status='auditing', audit_round=round_index)
            engine.write(workspace / 'run.json', manifest)
            publish('audit', round_index)
            engine.write(workspace / 'audit-input.json', payload)
            # The audit sees findings and source facts, never correction responses/reasoning.
            feedback = {'claim_checks': audit.get('claim_checks', []), 'findings': audit.get('findings', [])}
            engine.write(directory / 'audit-feedback.json', feedback if round_index else {})
            before = protected(workspace)
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
            phase = ('audit', round_index)
            attempt, receipt = None, {}
            raw, receipt = engine.scientific_session(workspace, 'audit', protocol.AUDIT_MODEL, protocol.AUDIT_EFFORT,
                prompt, protocol.audit_schema(), executable=executable, round_index=round_index)
            attempt = raw
            engine.check_cancelled()
            engine.wait_for_tools(os.environ.get('RUN_KEY'))
            if changed_inputs(workspace, before, 'audit', round_index):
                raise ValueError('Read-only audit changed protected scientific inputs')
            passages = [json.loads(line) for p in (workspace/'passages').glob('*.jsonl') for line in p.read_text().splitlines()]
            audit = audit_validation.bind(payload, raw, passages=passages)
            engine.write(workspace/'audit.json', audit)
            engine.write(workspace/'audit-receipt.json', receipt)  # Current projection; immutable original lives in its round.
            used = {p for c in audit.get('claim_checks', []) for p in c.get('passage_ids', [])}
            used.update(p for e in audit.get('evidence_checks', []) for p in e.get('passage_ids', []))
            used.update(p for f in audit.get('findings', []) for p in f.get('passage_ids', []))
            used.update(p for r in payload.get('study_results', []) for f in r.get('fields', {}).values() if f for p in f.get('passage_ids', []))
            history.append({'round': round_index, 'kind': 'audit', 'raw_audit': raw, 'audit': audit,
                'context_passages': [p for p in passages if p['id'] in used], 'receipt': receipt})
            engine.immutable_bytes(directory/'audit-bound.json', (json.dumps(audit, ensure_ascii=False, indent=2)+'\n').encode())
            flagged = corrections.targets(payload, audit)
            if not flagged or round_index == manifest['max_correction_rounds']:
                break
            next_round = round_index + 1
            manifest.update(status='correcting', correction_round=next_round)
            engine.write(workspace/'run.json', manifest)
            publish('correction', next_round)
            correction_input = {'question': manifest['question'], 'claim_ids': flagged, 'payload': payload,
                'claim_checks': audit['claim_checks'], 'findings': audit.get('findings', []),
                'claim_review_checks': payload.get('claim_review_checks', [])}
            engine.write(workspace/'rounds'/f'{next_round:03d}'/'correction-input.json', correction_input)
            before = protected(workspace)
            phase = ('correction', next_round)
            attempt, receipt = None, {}
            response, receipt = engine.scientific_session(workspace, 'correction', protocol.RESEARCH_MODEL, protocol.RESEARCH_EFFORT,
                protocol.correction_prompt(manifest['question'], command, next_round), corrections.schema(),
                executable=executable, round_index=next_round)
            attempt = response
            engine.check_cancelled()
            engine.wait_for_tools(os.environ.get('RUN_KEY'))
            if changed_inputs(workspace, before, 'correction', next_round):
                raise ValueError('Correction changed protected inputs instead of returning a typed revision')
            proposed, removed = corrections.apply(dict(payload, canonical_passage_ids=[p['id'] for p in passages]), audit, response)
            withdrawn.extend(removed)
            payload = engine.bind_payload(workspace, investigation=proposed)
            history.append({'round': next_round, 'kind': 'correction', 'responses': response['responses'],
                'investigation': proposed, 'payload': payload, 'receipt': receipt})
            engine.immutable_bytes(workspace/'rounds'/f'{next_round:03d}'/'subject.json',
                (json.dumps(payload, ensure_ascii=False, indent=2)+'\n').encode())
    except Exception as error:
        failure = type(error).__name__ + ': ' + str(error)
        manifest['verification_failure'] = failure
        engine.write(workspace/'run.json', manifest)
        kind, number = phase
        if not any(r['kind'] == kind and r['round'] == number for r in history):
            directory = workspace/'rounds'/f'{number:03d}'
            response_path = directory/(kind+'-response.json')
            if not response_path.is_file():
                response_path = directory/(kind+'.json')
            try:
                response_text = response_path.read_text(encoding='utf-8')
            except OSError:
                response_text = None
            history.append({'round': number, 'kind': kind, 'status': 'cancelled' if
                isinstance(error, engine.RunCancelled) or engine.cancellation_requested() else 'failed',
                'failure': failure, 'claim_ids': [c['id'] for c in payload.get('claims', [])],
                'receipt': engine.read(directory/(kind+'-receipt.json'), receipt), 'response_text': response_text,
                'raw_audit' if kind == 'audit' else 'response': attempt})
        # Leave no candidate silently accepted after a partial/failing correction.
        if (workspace/'investigation.json').read_bytes() != original:
            engine.immutable_bytes(workspace/'investigation-unexpected-change.json', (workspace/'investigation.json').read_bytes())
            (workspace/'investigation.json').write_bytes(original)
        if isinstance(error, engine.RunCancelled) or engine.cancellation_requested():
            final = corrections.finalize(payload, audit, history, withdrawn, failure=failure)
            engine.write(workspace/'payload.json', final)
            raise
    final = corrections.finalize(payload, audit, history, withdrawn, failure=failure)
    engine.write(workspace/'payload.json', final)
    engine.write(workspace/'claim-revisions.json', history)
    manifest['verification'] = {k: v for k, v in final['verification'].items() if k != 'audit_basis'}
    return final
