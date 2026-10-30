"""Lossless delivered JSON, independent of model behavior or scientific judgment."""
import copy
import json
import subprocess

import jsonschema
import pytest

from canary import bundle_connectors, bundle_encoding as codec
from test_compact_bundle import prepared


@pytest.fixture
def contribution(tmp_path):
    prepared(tmp_path)
    result = bundle_connectors.resolvent(tmp_path)
    text = 'Synthetic Unicode quotation — αβγ; tabs\tand newlines\n' * 50
    result['method_metadata']['a~/b'] = text
    repeated = {'reading': 'Synthetic repeated reading note. ' * 40, 'unknown': None, 'values': [1, 1.5, False]}
    result['method_metadata']['canary_view'] = {'first': repeated, 'second': copy.deepcopy(repeated),
        'quote': text, 'literal': [{codec.REF: '/not/a/reference'}, {codec.VALUE: 0},
                                  {codec.LITERAL: {codec.REF: '/still/literal'}}, {codec.NUMBER: 'literal'}]}
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
    artifact = contribution['result']['artifacts'][1]
    raw = json.loads(artifact['content'])
    raw['numeric_fixture'] = {'float': 1.0, 'large_integer': 9007199254740993, 'negative_zero': -0.0}
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
                                    'pointer', 'missing', 'artifact_index', 'duplicate_artifact', 'checksum'])
def test_invalid_reference_encoding_is_reported(contribution, failure):
    encoded = codec.encode(contribution)
    header = encoded['method_metadata']['canary_encoding']
    if failure == 'version': header['version'] = 2
    elif failure == 'bool_version': header['version'] = True
    elif failure == 'null_header': encoded['method_metadata']['canary_encoding'] = None
    elif failure == 'missing_view': del encoded['method_metadata']['canary_view']
    elif failure == 'index': encoded['method_metadata']['canary_view'] = {codec.VALUE: -1}
    elif failure == 'cycle':
        header['values'][0] = {codec.VALUE: 0}
        encoded['method_metadata']['canary_view'] = {codec.VALUE: 0}
    elif failure == 'pointer': encoded['method_metadata']['canary_view'] = {codec.REF: '/method_metadata/canary_view'}
    elif failure == 'missing': encoded['method_metadata']['canary_view'] = {codec.REF: '/sources/999999/excerpt'}
    elif failure == 'artifact_index': header['artifacts'][0]['index'] = -1
    elif failure == 'duplicate_artifact': header['artifacts'].append(copy.deepcopy(header['artifacts'][0]))
    elif failure == 'checksum': header['sha256'] = '0'*64
    with pytest.raises(ValueError): codec.expand(encoded)
