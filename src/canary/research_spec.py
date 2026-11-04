"""Small, side-effect-free research compiler owned by the HTTP service."""
from __future__ import annotations

import hashlib
import json
from typing import TypedDict
from pathlib import Path

import yaml

from . import coverage_protocol as protocol
from .research_state import validate as validate_state, MAX_DECLARATION_BYTES


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


def _declaration(source: str):
    if not isinstance(source, str) or not source.strip() or len(source.encode()) > MAX_DECLARATION_BYTES:
        raise CompileError('yaml', 'Expected a nonempty YAML declaration under 2 MiB.')
    try:
        draft = yaml.load(source, Loader=DeclarationLoader)
    except (yaml.YAMLError, RecursionError, TypeError):
        raise CompileError('yaml', 'Invalid declarative YAML; executable tags and recursive structures are unsupported.') from None
    return draft


def compile_research(source: str) -> CompiledResearch:
    return compile_mapping(_declaration(source), source=source)


def compile_mapping(draft, *, source: str | None = None) -> CompiledResearch:
    def block(value, allowed, path):
        if not isinstance(value, dict):
            raise CompileError(path, 'Expected an object.')
        if any(not isinstance(k, str) or k not in allowed for k in value):
            raise CompileError(path, 'Unknown fields are not accepted.')
        return value

    block(draft, {'version', 'question', 'research', 'audit', 'retrieval', 'subagents', 'research_state', 'continue_research'}, 'spec')
    version = draft.get('version', 1)
    if type(version) is not int or version != 1:
        raise CompileError('version', 'Only version 1 is supported.')
    question = draft.get('question')
    if not isinstance(question, str) or not question.strip() or len(question) > 20_000:
        raise CompileError('question', 'Expected a question of 1–20,000 characters.')
    try:
        state = validate_state(draft.get('research_state'), draft.get('continue_research', False))
    except ValueError as error:
        raise CompileError('research_state', str(error)) from None
    research = block(draft.get('research', {}), {'model', 'effort'}, 'research')
    audit = block(draft.get('audit', {}), {'model', 'effort', 'budget_seconds'}, 'audit')
    retrieval = block(draft.get('retrieval', {}), {'selection', 'target_papers', 'corpus', 'access', 'local_text', 'additional_sources'}, 'retrieval')
    from .source_adapters import validate_names
    try:
        additional = validate_names(retrieval.get('additional_sources', []))
    except ValueError as error:
        raise CompileError('retrieval.additional_sources', str(error)) from None
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
        if additional:
            raise CompileError('retrieval.additional_sources', 'Additional public sources require corpus: fresh.')
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
    target = retrieval.get('target_papers', 50)
    if type(target) is not int or target <= 0:
        raise CompileError('retrieval.target_papers', 'Expected a positive integer.')
    if audit.get('budget_seconds') is not None:
        raise CompileError('audit.budget_seconds', 'Audits have no time limit; omit this field or use null.')
    resolved = {'version': 1, 'question': question.strip(),
                'research': {'model': protocol.RESEARCH_MODEL, 'effort': protocol.RESEARCH_EFFORT},
                'audit': {'model': protocol.AUDIT_MODEL, 'effort': protocol.AUDIT_EFFORT, 'budget_seconds': None},
                'retrieval': {'selection': 'coverage', 'target_papers': target, 'corpus': corpus, 'access': 'open_full_text',
                              **({'local_text': dict(local)} if local is not None else {}),
                              **({'additional_sources': additional} if additional else {})},
                'subagents': False}
    if 'research_state' in draft:
        resolved['research_state'] = state
    if 'continue_research' in draft or state is not None:
        resolved['continue_research'] = draft.get('continue_research', False)
    return {'source_yaml': source if source is not None else yaml.safe_dump(draft, sort_keys=False, allow_unicode=True),
            'resolved': resolved, 'spec_sha256': digest(resolved), 'compiler_version': 1,
            'checks': ['strict_declaration', 'supported_research_and_audit', 'source_policy',
                       'positive_limits', 'unlimited_audit', 'no_subagents', 'api_owned_fresh_workspace']}


def verify_compiled(packet: dict) -> CompiledResearch:
    """Verify a frozen specification rather than replacing it with today's defaults."""
    if not isinstance(packet, dict) or packet.get('compiler_version') != 1:
        raise CompileError('compiler_version', 'Unsupported compiled specification.')
    original = compile_research(packet.get('source_yaml'))
    resolved = compile_mapping(packet.get('resolved'))
    if original['resolved'] != resolved['resolved'] or resolved['spec_sha256'] != packet.get('spec_sha256'):
        raise CompileError('spec_sha256', 'Declaration, resolved settings and digest disagree.')
    return original


def verify_recorded(packet: dict) -> CompiledResearch:
    """Validate saved inputs without imposing today's executable model defaults."""
    if not isinstance(packet, dict) or packet.get('compiler_version') != 1:
        raise CompileError('compiler_version', 'Unsupported compiled specification.')
    resolved = packet.get('resolved')
    if not isinstance(resolved, dict) or digest(resolved) != packet.get('spec_sha256'):
        raise CompileError('spec_sha256', 'Recorded settings and digest disagree.')
    draft = _declaration(packet.get('source_yaml'))
    if not isinstance(draft, dict) or not isinstance(draft.get('question'), str) or not draft['question'].strip():
        raise CompileError('question', 'A recorded research question is required.')

    # Omitted fields retain their saved defaults. Execution still uses verify_compiled.
    def compare(supplied, recorded, path=()):
        if isinstance(supplied, dict):
            if not isinstance(recorded, dict):
                raise CompileError('source_yaml', 'Recorded input structure disagrees with resolved settings.')
            for key, value in supplied.items():
                if key not in recorded:
                    # The compiler omits an explicitly empty optional-source list.
                    if (*path, key) == ('retrieval', 'additional_sources') and value == []:
                        continue
                    raise CompileError('source_yaml', 'Recorded input field is absent from resolved settings.')
                compare(value, recorded[key], (*path, key))
            return
        if path == ('question',):
            supplied = supplied.strip()
        if type(supplied) is not type(recorded) or supplied != recorded:
            raise CompileError('source_yaml', 'Recorded declaration disagrees with resolved settings.')

    compare(draft, resolved)
    return packet
