"""Review plans recorded when discovery accepts a draft."""
from pathlib import Path

from .research_spec import digest

from .coverage_runtime import now, read, write


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


def save_plan(workspace: Path, plan: dict) -> None:
    history = workspace / FILE
    current = workspace / 'question-plan.json'
    if not history.exists():
        previous = read(current)
        snapshot = previous or plan
        write(history, [dict(plan=snapshot, recorded_at=now(), original=previous is None,
                             outcomes=outcome_definitions(snapshot['outcomes']))])
    write(current, plan)
