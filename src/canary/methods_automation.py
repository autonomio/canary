"""Automation details supported by the saved session receipts."""
import hashlib
import json
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qsl, urlsplit


Record = TypeVar('Record', dict, list)

ROLES = {'research': 'Screening and extraction', 'audit': 'Audit of claims and evidence',
         'correction': 'Correction of disputed findings'}


def evaluation_url(value: object) -> str | None:
    """Return an HTTPS evaluation link suitable for publication."""
    if not isinstance(value, str) or not value.startswith('https://') or '\\' in value:
        return None
    try:
        url = urlsplit(value)
        private = {'access_token', 'token', 'api_key', 'key', 'signature', 'x-amz-signature'}
        if url.hostname and not url.username and not url.password and not any(
                key.lower() in private for key, _ in parse_qsl(url.query)):
            return value
    except ValueError:
        pass
    return None


def load_record(path: Path, default: Record) -> Record:
    """Read a JSON record of the expected type."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def sessions(workspace: Path) -> list[dict]:
    """Collect session receipts and evaluations covering the recorded instructions."""
    def saved(path: Path) -> bool:
        return path.is_file() and path.resolve().is_relative_to(workspace.resolve())

    result = []
    evaluations = workspace / 'methods-evaluations.json'
    references = load_record(evaluations, []) if saved(evaluations) else []
    rounds = sorted((workspace / 'rounds').glob('[0-9][0-9][0-9]'))
    for directory in [workspace, *rounds]:
        for role in ROLES:
            path = directory / f'{role}-receipt.json'
            if not saved(path):
                continue
            if role == 'audit' and directory == workspace and any(saved(d / path.name) for d in rounds):
                continue
            receipt = load_record(path, {})
            prompt_path = directory / f'{role}-prompt.txt'
            prompt = prompt_path.read_bytes() if saved(prompt_path) else None
            config = dict(role=role, tool='Codex CLI', version=receipt.get('cli_version'), model=receipt.get('model'),
                          effort=receipt.get('reasoning_effort'),
                          prompt_sha256=hashlib.sha256(prompt).hexdigest() if prompt is not None else None)
            follow_up = bool(receipt.get('response_retry') or receipt.get('capacity_pauses'))
            url = None if follow_up else next((evaluation_url(r.get('url')) for r in references
                        if isinstance(r, dict) and all(config.values()) and r.get('configuration') == config and evaluation_url(r.get('url'))), None)
            result.append(dict(configuration=config, round=directory.name if directory != workspace else None,
                               prompt=prompt.decode('utf-8', errors='replace') if prompt is not None else None,
                               evaluation=url, follow_up=follow_up, exit_code=receipt.get('exit_code')))
    return result
