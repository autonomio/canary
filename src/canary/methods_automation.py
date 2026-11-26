"""Automation details supported by the saved session receipts."""
import hashlib
import json
from urllib.parse import urlsplit


ROLES = {'research': 'Screening and extraction', 'audit': 'Audit of claims and evidence',
         'correction': 'Correction of disputed findings'}


def evaluation_url(value):
    if isinstance(value, str):
        url = urlsplit(value)
        if url.scheme == 'https' and url.hostname and not url.username and not url.password and not url.query:
            return value
    return None


def sessions(workspace):
    def saved(path):
        return path.is_file() and path.resolve().is_relative_to(workspace.resolve())

    result = []
    evaluations = workspace / 'methods-evaluations.json'
    references = json.loads(evaluations.read_text()) if saved(evaluations) else []
    rounds = sorted((workspace / 'rounds').glob('[0-9][0-9][0-9]'))
    for directory in [workspace, *rounds]:
        for role in ROLES:
            path = directory / f'{role}-receipt.json'
            if not saved(path):
                continue
            if role == 'audit' and directory == workspace and any(saved(d / path.name) for d in rounds):
                continue
            receipt = json.loads(path.read_text())
            prompt_path = directory / f'{role}-prompt.txt'
            prompt = prompt_path.read_bytes() if saved(prompt_path) else None
            config = dict(role=role, tool='Codex CLI', version=receipt.get('cli_version'), model=receipt.get('model'),
                          effort=receipt.get('reasoning_effort'),
                          prompt_sha256=hashlib.sha256(prompt).hexdigest() if prompt is not None else None)
            url = next((evaluation_url(r.get('url')) for r in references
                        if all(config.values()) and r.get('configuration') == config and evaluation_url(r.get('url'))), None)
            result.append(dict(configuration=config, round=directory.name if directory != workspace else None,
                               prompt=prompt.decode() if prompt is not None else None,
                               evaluation=url, exit_code=receipt.get('exit_code')))
    return result
