"""Review plans recorded when discovery accepts a draft."""
from pathlib import Path

from .coverage_runtime import now, read, write


FILE = 'review-protocol.json'


def save_plan(workspace: Path, plan: dict) -> None:
    history = workspace / FILE
    current = workspace / 'question-plan.json'
    if not history.exists():
        previous = read(current)
        write(history, [dict(plan=previous or plan, recorded_at=now(), original=previous is None)])
    write(current, plan)
