"""Small, side-effect-free research compiler owned by the HTTP service."""
from __future__ import annotations

import hashlib
import json
from typing import TypedDict
from pathlib import Path

import yaml

from . import coverage_protocol as protocol


class CompileError(ValueError):
    def __init__(self, path, message):
        self.issues = [{'path': path, 'message': message}]
        super().__init__(f'{path}: {message}')


class CompiledResearch(TypedDict):
    source_yaml: str
    resolved: dict
    spec_sha256: str
    compiler_version: int
    checks: list[str]


class DeclarationLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise CompileError('yaml', 'Mapping keys must be unique strings.')
        result[key] = loader.construct_object(value_node)
    return result


DeclarationLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def compile_research(source: str) -> CompiledResearch:
    if not isinstance(source, str) or not source.strip() or len(source.encode()) > 60_000:
        raise CompileError('yaml', 'Expected a nonempty YAML declaration under 60 KB.')
    try:
        draft = yaml.load(source, Loader=DeclarationLoader)
    except (yaml.YAMLError, RecursionError, TypeError):
        raise CompileError('yaml', 'Invalid declarative YAML; executable tags and recursive structures are unsupported.') from None
    return compile_mapping(draft, source=source)


def compile_mapping(draft, *, source: str | None = None) -> CompiledResearch:
    def block(value, allowed, path):
        if not isinstance(value, dict):
            raise CompileError(path, 'Expected an object.')
        if any(not isinstance(k, str) or k not in allowed for k in value):
            raise CompileError(path, 'Unknown fields are not accepted.')
        return value

    block(draft, {'version', 'question', 'research', 'audit', 'retrieval', 'subagents'}, 'spec')
    version = draft.get('version', 1)
    if type(version) is not int or version != 1:
        raise CompileError('version', 'Only version 1 is supported.')
    question = draft.get('question')
    if not isinstance(question, str) or not question.strip() or len(question) > 20_000:
        raise CompileError('question', 'Expected a question of 1–20,000 characters.')
    research = block(draft.get('research', {}), {'model', 'effort'}, 'research')
    audit = block(draft.get('audit', {}), {'model', 'effort', 'budget_seconds'}, 'audit')
    retrieval = block(draft.get('retrieval', {}), {'selection', 'target_papers', 'corpus', 'access', 'local_text'}, 'retrieval')
    for path, value, expected in (
        ('research.model', research.get('model', protocol.RESEARCH_MODEL), protocol.RESEARCH_MODEL),
        ('research.effort', research.get('effort', protocol.RESEARCH_EFFORT), protocol.RESEARCH_EFFORT),
        ('audit.model', audit.get('model', protocol.AUDIT_MODEL), protocol.AUDIT_MODEL),
        ('audit.effort', audit.get('effort', protocol.AUDIT_EFFORT), protocol.AUDIT_EFFORT),
        ('retrieval.selection', retrieval.get('selection', 'coverage'), 'coverage'),
        ('retrieval.access', retrieval.get('access', 'open_full_text'), 'open_full_text'),
    ):
        if value != expected:
            raise CompileError(path, f'This engine supports {expected}; no fallback is applied.')
    corpus = retrieval.get('corpus', 'fresh')
    if corpus not in ('fresh', 'local_text'):
        raise CompileError('retrieval.corpus', 'Supported corpora: fresh, local_text.')
    local = None
    if corpus == 'local_text':
        local = block(retrieval.get('local_text'), {'directory', 'metadata'}, 'retrieval.local_text')
        if 'directory' not in local:
            raise CompileError('retrieval.local_text.directory', 'An absolute server-side directory is required.')
        for field, value in local.items():
            if not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value or not Path(value).is_absolute():
                raise CompileError('retrieval.local_text.' + field, 'Expected an absolute server-side path.')
    elif 'local_text' in retrieval:
        raise CompileError('retrieval.local_text', 'Local text settings require corpus: local_text.')
    if draft.get('subagents', False) is not False:
        raise CompileError('subagents', 'Subagents are disabled for research and audit.')
    target, seconds = retrieval.get('target_papers', 50), audit.get('budget_seconds', 600)
    for path, value in [('retrieval.target_papers', target), ('audit.budget_seconds', seconds)]:
        if type(value) is not int or value <= 0:
            raise CompileError(path, 'Expected a positive integer.')
    resolved = {'version': 1, 'question': question.strip(),
                'research': {'model': protocol.RESEARCH_MODEL, 'effort': protocol.RESEARCH_EFFORT},
                'audit': {'model': protocol.AUDIT_MODEL, 'effort': protocol.AUDIT_EFFORT, 'budget_seconds': seconds},
                'retrieval': {'selection': 'coverage', 'target_papers': target, 'corpus': corpus, 'access': 'open_full_text',
                              **({'local_text': dict(local)} if local is not None else {})},
                'subagents': False}
    return {'source_yaml': source if source is not None else yaml.safe_dump(draft, sort_keys=False),
            'resolved': resolved, 'spec_sha256': digest(resolved), 'compiler_version': 1,
            'checks': ['strict_declaration', 'supported_research_and_audit', 'source_policy',
                       'positive_limits', 'no_subagents', 'api_owned_fresh_workspace']}


def verify_compiled(packet: dict) -> CompiledResearch:
    """Verify a frozen specification rather than replacing it with today's defaults."""
    if not isinstance(packet, dict) or packet.get('compiler_version') != 1:
        raise CompileError('compiler_version', 'Unsupported compiled specification.')
    original = compile_research(packet.get('source_yaml'))
    resolved = compile_mapping(packet.get('resolved'))
    if original['resolved'] != resolved['resolved'] or resolved['spec_sha256'] != packet.get('spec_sha256'):
        raise CompileError('spec_sha256', 'Declaration, resolved settings and digest disagree.')
    return original
