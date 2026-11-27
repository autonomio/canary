"""Review plans recorded when discovery accepts a draft."""
from pathlib import Path

from .coverage_runtime import now, read, write
from .research_spec import digest


FILE = 'review-protocol.json'


def outcome_definitions(outcomes: list) -> list[dict]:
    definitions = []
    for outcome in outcomes:
        fields = outcome if isinstance(outcome, dict) else {'name': outcome}
        if not isinstance(fields.get('name'), str) or not fields['name'].strip():
            continue
        definition = {key: fields.get(key) for key in ('name', 'unit', 'time_point', 'effect_measure')}
        definitions.append(dict(definition, id=digest(definition)))
    return definitions


def _record(plan: dict, history: list, *, author: str, reason: str | None,
            original: bool, stage: str, recorded_at: str | None = None) -> dict:
    previous = history[-1] if history else None
    old_plan = previous['plan'] if previous else {}
    changed = sorted(key for key in old_plan.keys() | plan.keys() if old_plan.get(key) != plan.get(key))
    version = dict(number=len(history) + 1, recorded_at=recorded_at or now(), author=author,
                   original=original, stage=stage, reason=reason, changed=changed if previous else [],
                   previous=previous['sha256'] if previous else None, plan=plan,
                   outcomes=outcome_definitions(plan.get('outcomes', [])))
    return dict(version, sha256=digest(version))


def versions(path: Path) -> list[dict]:
    history = read(path, [])
    parent = None
    for number, version in enumerate(history, 1):
        content = {key: value for key, value in version.items() if key != 'sha256'}
        if (version['number'] != number or version['previous'] != parent
                or digest(content) != version['sha256']):
            raise ValueError('The saved review protocol has changed')
        parent = version['sha256']
    return history


def _comparable(plan: dict) -> dict:
    queries = plan.get('queries')
    if isinstance(queries, list) and all(isinstance(q, dict) and isinstance(q.get('id'), str) for q in queries):
        return dict(plan, queries=sorted(queries, key=lambda q: q['id']))
    return plan


def history_file(workspace: Path | None, inventory: list[dict] | None = None) -> Path | None:
    """Locate the protocol history, verified against a sealed inventory when one is given."""
    from .methods_search import working_file
    path = working_file(workspace, FILE)
    if inventory is None or workspace is None:
        return path
    from .research_bundle import verified_artifact
    records = {record['path']: record for record in inventory}
    for name in (FILE, 'common/' + FILE, 'research/' + FILE):
        if name in records:
            return verified_artifact(workspace, records[name])
    if path is not None:
        raise ValueError('The review protocol is missing from the sealed bundle inventory')
    return None


def save_plan(workspace: Path, plan: dict) -> None:
    path = workspace / FILE
    current = workspace / 'question-plan.json'
    history = versions(path)
    run = read(workspace / 'run.json', {})
    previous = read(current) if not history else None
    if previous:
        history.append(_record(previous, history, author='Not recorded', reason=None,
                               original=False, stage='Earlier plan; original timing unknown'))
    content = {k: v for k, v in plan.items() if k != 'amendment_reason'}
    changed = not history or _comparable(history[-1]['plan']) != _comparable(content)
    if changed:
        reason = plan.get('amendment_reason') if history else None
        if history and run.get('protocol_version') and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError('Give an amendment_reason explaining the change to the review plan')
        progress = read(workspace / 'search-progress.json', {})
        if any((workspace / name).exists() for name in ('selection.json', 'reviewed-papers.json')):
            stage = 'After screening began'
        elif progress.get('unique_candidates') or any(q.get('pages') or q.get('complete')
                                                      for q in progress.get('queries', {}).values()):
            stage = 'After search results were available'
        else:
            stage = 'Before recorded search results'
        author = read(workspace / 'run-spec.json', {}).get('protocol_author') or 'Not recorded'
        history.append(_record(content, history, author=author, reason=reason, original=not history, stage=stage))
    if previous or changed:
        write(path, history)
    write(current, history[-1]['plan'])


def approval_records(history: list[dict], entries: list[dict]) -> list[dict]:
    from .review_history import subject_id
    subjects = {}
    replaced = {row['replaces'] for row in entries if row.get('replaces')}
    versions_by_subject = {'protocol:' + version['sha256']: version['sha256'] for version in history}
    records = []
    for row in entries:
        subject = subject_id(row, subjects.get(row.get('replaces'), row['id']))
        subjects[row['id']] = subject
        decision = row['decision']
        if subject in versions_by_subject and row['id'] not in replaced:
            records.append(dict(version=versions_by_subject[subject], recorded_at=row['recorded_at'],
                                decision=decision, entry_id=row['id']))
    return records
