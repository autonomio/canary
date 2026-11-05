"""Lossless references in Canary extensions; native Resolvent records stay plain."""
from __future__ import annotations

import base64
import binascii
from collections import Counter
import copy
import hashlib
import json
import math
from typing import Literal, TypedDict
import zlib

FORMAT = 'canary.references'
# Version 2 deflates the version 1 packing (value table, artifacts, view) into one stream.
VERSION = 2
REF, VALUE, LITERAL = '$canary_ref', '$canary_value', '$canary_literal'
NUMBER = '$canary_number'
OBJECT = '$canary_object'
JS_SAFE_INTEGER = 2**53-1
ARTIFACTS = {'canary-bundle.json', 'canary-research.json'}
COMPRESSION = 'deflate'
PACKED_LIMIT = 256 * 1024 * 1024  # A bundle file is untrusted input: never inflate it beyond this.
PACKED = ('Packed Canary JSON. method_metadata.canary_encoding holds it; read it with '
          'canary.bundle_encoding.expand or the expanded export.')


class BundleEncoding(TypedDict):
    format: Literal['canary.references']
    version: Literal[1, 2]
    stored_bytes: int
    expanded_bytes: int


class EncodedArtifact(TypedDict):
    index: int
    name: Literal['canary-bundle.json', 'canary-research.json']
    style: Literal['default', 'compact']


class _ReferenceEncoding(TypedDict):
    format: Literal['canary.references']
    version: Literal[1, 2]
    artifacts: list[EncodedArtifact]
    view: bool
    expanded_bytes: int
    sha256: str


class ReferenceEncoding(_ReferenceEncoding, total=False):
    values: list[object]  # Version 1: the shared value table, inline.
    compression: Literal['deflate']  # Version 2: zlib stream, base64 in data.
    packed_bytes: int  # Version 2: exact inflated size of data.
    data: str


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _key(value):
    if isinstance(value, (str, dict, list)):
        body = dumps(value).encode('utf-8')
        if len(body) >= 100:
            return hashlib.sha256(body).digest()
    return None


def _js_reorders(node):
    keys = list(node)
    indices = [key for key in keys if isinstance(key, str) and len(key) <= 10 and key.isascii() and key.isdigit()
               and str(int(key)) == key and int(key) < 2**32-1]
    index_set = set(indices)
    return keys != sorted(indices, key=int) + [key for key in keys if key not in index_set]


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
            type(item.get('version')) is not int or item['version'] not in (1, VERSION) or
            not isinstance(item.get('artifacts'), list) or
            type(item.get('view')) is not bool or type(item.get('expanded_bytes')) is not int or
            item['expanded_bytes'] < 0 or not isinstance(item.get('sha256'), str) or
            (item['version'] == 1 and (not isinstance(item.get('values'), list) or
                                       (item['view'] and 'canary_view' not in metadata))) or
            (item['version'] == VERSION and (item.get('compression') != COMPRESSION or
                not isinstance(item.get('data'), str) or type(item.get('packed_bytes')) is not int or
                not 0 <= item['packed_bytes'] <= PACKED_LIMIT))):
        raise ValueError('Unsupported Canary bundle encoding')
    return item


def _packed(item):
    """Inflate version 2 data to exactly its recorded size, never more."""
    try:
        stream = zlib.decompressobj()
        body = stream.decompress(base64.b64decode(item['data'], validate=True), item['packed_bytes'] + 1)
        if len(body) != item['packed_bytes'] or not stream.eof or stream.unused_data:
            raise ValueError('size')
        packed = json.loads(body)
    except (binascii.Error, zlib.error, ValueError):
        raise ValueError('Invalid packed Canary bundle data') from None
    if (not isinstance(packed, dict) or set(packed) - {'view'} != {'values', 'artifacts'} or
            not isinstance(packed['values'], list) or not isinstance(packed['artifacts'], list) or
            len(packed['artifacts']) != len(item['artifacts']) or ('view' in packed) != item['view']):
        raise ValueError('Invalid packed Canary bundle data')
    return packed


def encode(value):
    """Return a smaller contribution when possible; never change the input."""
    encoding = header(value)
    if encoding is not None:
        expanded = expand(value)  # Re-export only a supported, reconstructible contribution.
        # An earlier version is packed again; its native records stay as recorded.
        return value if encoding['version'] == VERSION else encode(expanded)
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
        # A browser may normalize 1.0/-0.0 or round large integers in native
        # JSON. Encoded extensions must retain their original numeric spelling.
        if type(node) is float or (type(node) is int and abs(node) > JS_SAFE_INTEGER):
            return False
        safe = not (isinstance(node, dict) and _js_reorders(node))
        if isinstance(node, dict):
            for name, child in node.items():
                if (path == ('result',) and name == 'artifacts'):
                    continue
                safe = collect(child, (*path, name)) and safe
        elif isinstance(node, list):
            for index, child in enumerate(node):
                safe = collect(child, (*path, str(index))) and safe
        if safe and _immutable(path):
            key = _key(node)
            if key is not None:
                native.setdefault(key, '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in path))
        return safe
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
            if _js_reorders(node):
                return {OBJECT: [[key, pack(child)] for key, child in node.items()]}
            encoded = {key: pack(child) for key, child in node.items()}
            return {LITERAL: encoded} if len(node) == 1 and next(iter(node)) in (REF, VALUE, LITERAL, NUMBER, OBJECT) else encoded
        if isinstance(node, list):
            return [pack(child) for child in node]
        return node

    def pack(node):
        if type(node) is float or (type(node) is int and abs(node) > JS_SAFE_INTEGER):
            return {NUMBER: json.dumps(node, allow_nan=False)}
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

    packed = {'values': values, 'artifacts': [pack(content) for content in contents]}
    if has_view:
        packed['view'] = pack(view)
    body = dumps(packed).encode('utf-8')
    if len(body) > PACKED_LIMIT:
        return value
    for artifact in artifacts:
        result['result']['artifacts'][artifact['index']]['content'] = PACKED
    metadata['canary_encoding'] = {'format': FORMAT, 'version': VERSION, 'compression': COMPRESSION,
        'packed_bytes': len(body), 'data': base64.b64encode(zlib.compress(body, 9)).decode('ascii'),
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
    if encoding['version'] == 1:
        values, view = encoding['values'], value['method_metadata'].get('canary_view')
        contents = None  # Version 1 artifacts hold their packed JSON inline.
    else:
        packed = _packed(encoding)
        values, view, contents = packed['values'], packed.get('view'), packed['artifacts']
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
            if set(node) == {OBJECT}:
                if not isinstance(node[OBJECT], list):
                    raise ValueError('Invalid ordered Canary object')
                result = {}
                for pair in node[OBJECT]:
                    if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str) or pair[0] in result:
                        raise ValueError('Invalid ordered Canary object entry')
                    result[pair[0]] = unpack(pair[1])
                return result
            if set(node) == {NUMBER}:
                if not isinstance(node[NUMBER], str):
                    raise ValueError('Invalid encoded Canary number')
                number = json.loads(node[NUMBER])
                if type(number) not in (int, float) or (type(number) is float and not math.isfinite(number)):
                    raise ValueError('Invalid encoded Canary number')
                return number
            if set(node) == {REF}:
                return pointer(node[REF])
            if set(node) == {VALUE}:
                index = node[VALUE]
                if type(index) is not int or not 0 <= index < len(values):
                    raise ValueError('Invalid Canary bundle value index')
                if index in active:
                    raise ValueError('Cyclic Canary bundle value reference')
                if index not in memo:
                    active.add(index)
                    memo[index] = unpack(values[index])
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
    for position, item in enumerate(encoding['artifacts']):
        if (not isinstance(item, dict) or type(item.get('index')) is not int or
                not 0 <= item['index'] < len(result['result']['artifacts']) or item['index'] in seen or
                item.get('name') not in ARTIFACTS or item.get('style') not in ('default', 'compact')):
            raise ValueError('Invalid encoded Canary artifact')
        seen.add(item['index'])
        artifact = result['result']['artifacts'][item['index']]
        if artifact['name'] != item['name']:
            raise ValueError('Encoded Canary artifact name changed')
        content = unpack(json.loads(artifact['content']) if contents is None else contents[position])
        artifact['content'] = json.dumps(content, ensure_ascii=False) if item['style'] == 'default' else dumps(content)
    if encoding['view']:
        result['method_metadata']['canary_view'] = unpack(view)
    if _digest(_extensions(result, encoding['artifacts'], encoding['view'])) != encoding['sha256']:
        raise ValueError('Canary bundle reconstruction checksum mismatch')
    del result['method_metadata']['canary_encoding']
    return result
