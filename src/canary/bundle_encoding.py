"""Lossless references in Canary extensions; native Resolvent records stay plain."""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
from typing import Literal, TypedDict

FORMAT = 'canary.references'
VERSION = 1
REF, VALUE, LITERAL = '$canary_ref', '$canary_value', '$canary_literal'
ARTIFACTS = {'canary-bundle.json', 'canary-research.json'}


class BundleEncoding(TypedDict):
    format: Literal['canary.references']
    version: Literal[1]
    stored_bytes: int
    expanded_bytes: int


class EncodedArtifact(TypedDict):
    index: int
    name: Literal['canary-bundle.json', 'canary-research.json']
    style: Literal['default', 'compact']


class ReferenceEncoding(TypedDict):
    format: Literal['canary.references']
    version: Literal[1]
    values: list[object]
    artifacts: list[EncodedArtifact]
    view: bool
    expanded_bytes: int
    sha256: str


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _key(value):
    if isinstance(value, (str, dict, list)):
        body = dumps(value).encode('utf-8')
        if len(body) >= 100:
            return hashlib.sha256(body).digest()
    return None


def _immutable(path):
    if not path:
        return False
    if path[0] == 'result':
        return len(path) >= 2 and path[1] != 'artifacts'
    if path[0] == 'method_metadata':
        return len(path) >= 2 and path[1] not in ('canary_view', 'canary_encoding')
    return True


def _extensions(value, artifacts, has_view):
    result = {'artifacts': [value['result']['artifacts'][a['index']]['content'] for a in artifacts]}
    if has_view:
        result['view'] = value['method_metadata']['canary_view']
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def header(value) -> ReferenceEncoding | None:
    metadata = value.get('method_metadata', {})
    if not isinstance(metadata, dict):
        raise ValueError('Invalid Canary bundle metadata')
    if 'canary_encoding' not in metadata:
        return None
    item = metadata['canary_encoding']
    if (not isinstance(item, dict) or item.get('format') != FORMAT or
            type(item.get('version')) is not int or item['version'] != VERSION or
            not isinstance(item.get('values'), list) or not isinstance(item.get('artifacts'), list) or
            type(item.get('view')) is not bool or type(item.get('expanded_bytes')) is not int or
            item['expanded_bytes'] < 0 or not isinstance(item.get('sha256'), str) or
            (item['view'] and 'canary_view' not in metadata)):
        raise ValueError('Unsupported Canary bundle encoding')
    return item


def encode(value):
    """Return a smaller contribution when possible; never change the input."""
    if header(value) is not None:
        expand(value)  # Re-export only a supported, reconstructible contribution.
        return value
    result = copy.deepcopy(value)
    metadata = result['method_metadata']
    has_view = 'canary_view' in metadata
    view = metadata.pop('canary_view', None)
    artifacts, contents = [], []
    for index, item in enumerate(result['result']['artifacts']):
        if item.get('name') not in ARTIFACTS:
            continue
        try:
            parsed = json.loads(item['content'])
        except (ValueError, TypeError):
            continue  # Unrecognized content is kept verbatim, never repaired.
        style = next((style for style in ('default', 'compact') if
            (json.dumps(parsed, ensure_ascii=False) if style == 'default' else dumps(parsed)) == item['content']), None)
        if style is not None:
            artifacts.append({'index': index, 'name': item['name'], 'style': style})
            contents.append(parsed)

    native, counts, values, identifiers = {}, Counter(), [], {}
    def collect(node, path=()):
        if _immutable(path):
            key = _key(node)
            if key is not None:
                native.setdefault(key, '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in path))
        if isinstance(node, dict):
            for name, child in node.items():
                if (path == ('result',) and name == 'artifacts'):
                    continue
                collect(child, (*path, name))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                collect(child, (*path, str(index)))
    collect(result)

    def count(node):
        key = _key(node)
        if key is not None:
            counts[key] += 1
        if isinstance(node, dict):
            for child in node.values():
                count(child)
        elif isinstance(node, list):
            for child in node:
                count(child)
    count([contents, view])

    def children(node):
        if isinstance(node, dict):
            encoded = {key: pack(child) for key, child in node.items()}
            return {LITERAL: encoded} if len(node) == 1 and next(iter(node)) in (REF, VALUE, LITERAL) else encoded
        if isinstance(node, list):
            return [pack(child) for child in node]
        return node

    def pack(node):
        key = _key(node)
        if key in native:
            return {REF: native[key]}
        if key is not None and counts[key] > 1:
            if key not in identifiers:
                identifiers[key] = len(values)
                values.append(None)
                values[identifiers[key]] = children(node)
            return {VALUE: identifiers[key]}
        return children(node)

    for artifact, content in zip(artifacts, contents):
        result['result']['artifacts'][artifact['index']]['content'] = dumps(pack(content))
    if has_view:
        metadata['canary_view'] = pack(view)
    metadata['canary_encoding'] = {'format': FORMAT, 'version': VERSION, 'values': values,
        'artifacts': artifacts, 'view': has_view, 'expanded_bytes': len(dumps(value).encode('utf-8')),
        'sha256': _digest(_extensions(value, artifacts, has_view))}
    if len(dumps(result).encode('utf-8')) >= metadata['canary_encoding']['expanded_bytes']:
        return value
    if expand(result) != value:
        raise ValueError('Canary bundle encoding changed the original content')
    return result


def expand(value):
    """Reconstruct original extension values and embedded JSON strings exactly.

    References are in-document JSON pointers, never URLs or filesystem paths.
    Added consumer metadata is permitted; reconstructed Canary content is hashed.
    """
    encoding = header(value)
    if encoding is None:
        return value
    active, memo = set(), {}

    def pointer(path):
        if not isinstance(path, str) or not path.startswith('/'):
            raise ValueError('Invalid Canary bundle reference')
        parts = tuple(part.replace('~1', '/').replace('~0', '~') for part in path[1:].split('/'))
        if not _immutable(parts):
            raise ValueError('Canary bundle reference targets encoded content')
        result = value
        for part in parts:
            if isinstance(result, list):
                if not part.isascii() or not part.isdigit() or int(part) >= len(result):
                    raise ValueError('Invalid Canary bundle reference index')
                result = result[int(part)]
            elif isinstance(result, dict) and part in result:
                result = result[part]
            else:
                raise ValueError('Missing Canary bundle reference')
        return copy.deepcopy(result)

    def unpack(node):
        if isinstance(node, dict):
            if set(node) == {REF}:
                return pointer(node[REF])
            if set(node) == {VALUE}:
                index = node[VALUE]
                if type(index) is not int or not 0 <= index < len(encoding['values']):
                    raise ValueError('Invalid Canary bundle value index')
                if index in active:
                    raise ValueError('Cyclic Canary bundle value reference')
                if index not in memo:
                    active.add(index)
                    memo[index] = unpack(encoding['values'][index])
                    active.remove(index)
                return copy.deepcopy(memo[index])
            if set(node) == {LITERAL}:
                if not isinstance(node[LITERAL], dict):
                    raise ValueError('Invalid Canary bundle literal')
                node = node[LITERAL]
            return {key: unpack(child) for key, child in node.items()}
        if isinstance(node, list):
            return [unpack(child) for child in node]
        return node

    result = copy.deepcopy(value)
    seen = set()
    for item in encoding['artifacts']:
        if (not isinstance(item, dict) or type(item.get('index')) is not int or
                not 0 <= item['index'] < len(result['result']['artifacts']) or item['index'] in seen or
                item.get('name') not in ARTIFACTS or item.get('style') not in ('default', 'compact')):
            raise ValueError('Invalid encoded Canary artifact')
        seen.add(item['index'])
        artifact = result['result']['artifacts'][item['index']]
        if artifact['name'] != item['name']:
            raise ValueError('Encoded Canary artifact name changed')
        content = unpack(json.loads(artifact['content']))
        artifact['content'] = json.dumps(content, ensure_ascii=False) if item['style'] == 'default' else dumps(content)
    if encoding['view']:
        result['method_metadata']['canary_view'] = unpack(value['method_metadata']['canary_view'])
    if _digest(_extensions(result, encoding['artifacts'], encoding['view'])) != encoding['sha256']:
        raise ValueError('Canary bundle reconstruction checksum mismatch')
    del result['method_metadata']['canary_encoding']
    return result
