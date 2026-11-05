"""Lossless delivered JSON, independent of model behavior or scientific judgment."""
import base64
import copy
import json
import subprocess
import zlib

import jsonschema
import pytest

from canary import bundle_connectors, bundle_encoding as codec
from test_compact_bundle import prepared


def packed(encoded):
    return json.loads(zlib.decompress(base64.b64decode(encoded['method_metadata']['canary_encoding']['data'])))


def repack(encoded, edit):
    """Edit version 2 packing as a damaged or hostile file could."""
    header = encoded['method_metadata']['canary_encoding']
    value = packed(encoded)
    edit(value)
    body = codec.dumps(value).encode()
    header.update(packed_bytes=len(body), data=base64.b64encode(zlib.compress(body)).decode())


def as_version_1(encoded):
    """The same packing laid out inline, as version 1 stored it."""
    result = copy.deepcopy(encoded)
    header, value = result['method_metadata']['canary_encoding'], packed(encoded)
    for item, content in zip(header['artifacts'], value['artifacts']):
        result['result']['artifacts'][item['index']]['content'] = codec.dumps(content)
    if header['view']:
        result['method_metadata']['canary_view'] = value['view']
    for name in ('compression', 'packed_bytes', 'data'):
        del header[name]
    header.update(version=1, values=value['values'])
    return result


@pytest.fixture
def contribution(tmp_path):
    prepared(tmp_path)
    result = bundle_connectors.resolvent(tmp_path)
    text = 'Synthetic Unicode quotation — αβγ; tabs\tand newlines\n' * 50
    result['method_metadata']['a~/b'] = text
    repeated = {'reading': 'Synthetic repeated reading note. ' * 40, 'unknown': None, 'values': [1, 1.5, False]}
    result['method_metadata']['canary_view'] = {'first': repeated, 'second': copy.deepcopy(repeated),
        'quote': text, 'literal': [{codec.REF: '/not/a/reference'}, {codec.VALUE: 0},
                                  {codec.LITERAL: {codec.REF: '/still/literal'}}, {codec.NUMBER: 'literal'},
                                  {codec.OBJECT: 'literal'}]}
    return result


def test_reference_roundtrip_keeps_every_value_and_native_contract(contribution):
    original = copy.deepcopy(contribution)
    encoded = codec.encode(contribution)
    assert codec.header(encoded)['format'] == 'canary.references'
    assert len(codec.dumps(encoded).encode()) < len(codec.dumps(original).encode())
    assert codec.expand(json.loads(codec.dumps(encoded))) == original
    assert contribution == original
    assert codec.encode(encoded) == encoded
    for field in ('claims', 'evidence', 'sources'):
        assert encoded[field] == original[field]
    from pathlib import Path
    schema = json.loads((Path(__file__).parent/'fixtures/resolvent-submission.schema.json').read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(encoded)


def test_expansion_accepts_consumer_context_and_does_not_share_mutable_aliases(contribution):
    encoded = codec.encode(contribution)
    encoded['method_metadata']['consumer_context'] = {'question_snapshot': 'Synthetic extra context'}
    expected = copy.deepcopy(contribution)
    expected['method_metadata']['consumer_context'] = encoded['method_metadata']['consumer_context']
    decoded = codec.expand(encoded)
    assert decoded == expected
    decoded['method_metadata']['canary_view']['first']['values'].append(2)
    assert decoded['method_metadata']['canary_view']['second']['values'] == [1, 1.5, False]
    assert codec.expand(encoded) == expected


def test_browser_json_roundtrip_preserves_original_numbers_and_artifact_strings(contribution):
    numbers = {'float': 1.0, 'negative_zero': -0.0, 'padding': 'Synthetic native floating point. '*20}
    contribution['method_metadata']['native_float_fixture'] = numbers
    view = contribution['method_metadata']['canary_view']
    view['floats'] = copy.deepcopy(numbers)
    view['large_integer'] = 9007199254740993
    ordered = {'10': 'First numeric key. '*20, '2': 'Second numeric key. '*20, 'tail': 'Keep insertion order.'}
    contribution['method_metadata']['native_order_fixture'] = ordered
    view['ordered_fixture'] = copy.deepcopy(ordered)
    artifact = contribution['result']['artifacts'][1]
    raw = json.loads(artifact['content'])
    raw['numeric_fixture'] = {'float': 1.0, 'large_integer': 9007199254740993, 'negative_zero': -0.0}
    raw['ordered_fixture'] = copy.deepcopy(ordered)
    artifact['content'] = json.dumps(raw, ensure_ascii=False)
    encoded = codec.encode(contribution)
    browser = subprocess.run(['node', '-e',
        "let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',x=>s+=x);"
        "process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(s))));"],
        input=codec.dumps(encoded), text=True, capture_output=True, timeout=15, check=True)
    decoded = codec.expand(json.loads(browser.stdout))
    assert decoded == contribution
    assert decoded['result']['artifacts'][1]['content'] == artifact['content']


def test_unrecognized_artifact_format_is_preserved_byte_for_byte(contribution):
    content = json.dumps({'format': 'Synthetic pretty JSON', 'verbatim': 'α\nβ'}, ensure_ascii=False, indent=2)+'\n'
    contribution['result']['artifacts'][0]['content'] = content
    encoded = codec.encode(contribution)
    assert encoded['result']['artifacts'][0]['content'] == content
    assert codec.expand(encoded) == contribution


def test_legacy_and_small_contributions_need_no_encoding():
    plain = {'result': {'artifacts': []}, 'method_metadata': {}}
    assert codec.encode(plain) == plain
    assert codec.expand(plain) == plain
    assert codec.header(plain) is None


def test_multimegabyte_repetition_is_reconstructed_without_shortening(contribution):
    text = 'Synthetic large exact value — ' * 18000
    contribution['method_metadata']['large_fixture'] = text
    contribution['method_metadata']['canary_view']['large_copies'] = [text]*6
    assert len(codec.dumps(contribution).encode()) > 3_000_000
    encoded = codec.encode(contribution)
    assert len(codec.dumps(encoded).encode()) < 2_097_152
    assert codec.expand(encoded) == contribution


def test_large_native_record_is_complete_even_when_it_cannot_shrink():
    text = 'Synthetic native value. ' * 140000
    plain = {'result': {'artifacts': []}, 'method_metadata': {},
             'sources': [{'id': 's1', 'excerpt': text}], 'claims': [], 'evidence': []}
    assert len(codec.dumps(plain).encode()) > 3_000_000
    encoded = codec.encode(plain)
    assert encoded == plain and encoded['sources'][0]['excerpt'] == text


@pytest.mark.parametrize('failure', ['version', 'bool_version', 'null_header', 'missing_view', 'index', 'cycle',
                                    'pointer', 'missing', 'artifact_index', 'duplicate_artifact', 'checksum',
                                    'base64', 'trailing_data', 'larger_than_recorded', 'smaller_than_recorded',
                                    'beyond_limit', 'v1_missing_view', 'v1_cycle'])
def test_invalid_reference_encoding_is_reported(contribution, failure):
    encoded = codec.encode(contribution)
    if failure.startswith('v1_'):
        encoded = as_version_1(encoded)
    header = encoded['method_metadata']['canary_encoding']
    stream = base64.b64decode(header['data']) if 'data' in header else b''
    def view(value):
        return lambda packing: packing.update(view=value)
    def cycle(packing):
        packing['values'][0] = {codec.VALUE: 0}
        packing['view'] = {codec.VALUE: 0}
    if failure == 'version': header['version'] = 3
    elif failure == 'bool_version': header['version'] = True
    elif failure == 'null_header': encoded['method_metadata']['canary_encoding'] = None
    elif failure == 'missing_view': repack(encoded, lambda packing: packing.pop('view'))
    elif failure == 'index': repack(encoded, view({codec.VALUE: -1}))
    elif failure == 'cycle': repack(encoded, cycle)
    elif failure == 'pointer': repack(encoded, view({codec.REF: '/method_metadata/canary_view'}))
    elif failure == 'missing': repack(encoded, view({codec.REF: '/sources/999999/excerpt'}))
    elif failure == 'artifact_index': header['artifacts'][0]['index'] = -1
    elif failure == 'duplicate_artifact': header['artifacts'].append(copy.deepcopy(header['artifacts'][0]))
    elif failure == 'checksum': header['sha256'] = '0'*64
    elif failure == 'base64': header['data'] = '!' + header['data']
    elif failure == 'trailing_data': header['data'] = base64.b64encode(stream + b'trailing').decode()
    elif failure == 'larger_than_recorded': header['packed_bytes'] -= 1
    elif failure == 'smaller_than_recorded': header['packed_bytes'] += 1
    elif failure == 'beyond_limit': header['packed_bytes'] = codec.PACKED_LIMIT + 1
    elif failure == 'v1_missing_view': del encoded['method_metadata']['canary_view']
    elif failure == 'v1_cycle':
        header['values'][0] = {codec.VALUE: 0}
        encoded['method_metadata']['canary_view'] = {codec.VALUE: 0}
    with pytest.raises(ValueError): codec.expand(encoded)


def test_packed_data_cannot_inflate_beyond_its_recorded_size(contribution):
    encoded = codec.encode(contribution)
    header = encoded['method_metadata']['canary_encoding']
    bomb = zlib.compress(b' ' * 2 * 1024 * 1024, 9)  # 2 MiB from about 2 KB.
    header.update(data=base64.b64encode(bomb).decode(), packed_bytes=1024)
    with pytest.raises(ValueError, match='Invalid packed'):
        codec.expand(encoded)


def test_version_1_remains_readable_and_is_packed_again_on_reexport(contribution):
    encoded = codec.encode(contribution)
    earlier = as_version_1(encoded)
    assert codec.header(earlier)['version'] == 1
    assert codec.expand(json.loads(codec.dumps(earlier))) == contribution
    again = codec.encode(earlier)
    assert codec.header(again)['version'] == codec.VERSION
    assert codec.expand(again) == contribution
    assert len(codec.dumps(again)) < len(codec.dumps(earlier))
    for field in ('claims', 'evidence', 'sources'):
        assert again[field] == earlier[field]
