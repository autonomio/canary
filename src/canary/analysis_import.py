"""Analyses run outside Canary, kept byte for byte and checked against the study table they claim to use."""
import hashlib
import json
from pathlib import Path

import jsonschema

from .coverage_runtime import now, read, write
from .meta_analysis import MODELS
from .study_table import _outcome_name, _rows, _text, _unit

FILE = 'analysis-imports.json'
FOLDER = 'analysis-imports'
NUMBER = {'type': 'number'}
MAYBE = {'anyOf': [NUMBER, {'type': 'null'}]}
TEXT = {'type': 'string', 'minLength': 1}
SCHEMA = {'type': 'object', 'additionalProperties': False,
          'required': ['synthesis_id', 'outcome', 'unit', 'time_point', 'effect_measure', 'software', 'software_version',
                       'inputs', 'analyses'],
          'properties': {
              'synthesis_id': TEXT, 'outcome': TEXT, 'unit': TEXT, 'time_point': TEXT, 'effect_measure': TEXT,
              'software': TEXT, 'software_version': TEXT,
              'inputs': {'type': 'array', 'minItems': 1, 'items': {
                  'type': 'object', 'additionalProperties': False,
                  'required': ['study_id', 'study_result_id', 'estimate', 'standard_error'],
                  'properties': {'study_id': TEXT, 'study_result_id': TEXT, 'estimate': NUMBER, 'standard_error': NUMBER}}},
              'analyses': {'type': 'array', 'minItems': 1, 'items': {
                  'type': 'object', 'additionalProperties': False,
                  'required': ['model', 'estimate', 'lower', 'upper', 'q', 'i2', 'tau2'],
                  'properties': {'model': {'enum': list(MODELS)}, 'estimate': NUMBER, 'lower': NUMBER, 'upper': NUMBER,
                                 'q': MAYBE, 'i2': MAYBE, 'tau2': MAYBE}}}}}


def parse(content):
    """The analysis an import file describes, or ValueError naming what is wrong with it."""
    try:
        data = json.loads(content)
    except ValueError as error:
        raise ValueError(f'The analysis file is not JSON: {error}') from None
    errors = sorted(jsonschema.Draft202012Validator(SCHEMA).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        place = '/'.join(str(p) for p in errors[0].path)
        raise ValueError(f'The analysis file does not match the import format at {place or "the top level"}: {errors[0].message}')
    return data


def history(workspace):
    imports = read(Path(workspace) / FILE, [])
    return imports if isinstance(imports, list) else []


def add(workspace, synthesis_ids, file_name, content):
    """Store an import file unchanged with the study table edition it was checked against; the same file is stored once."""
    workspace = Path(workspace)
    data = parse(content)
    if data['synthesis_id'] not in synthesis_ids:
        raise LookupError(f"Synthesis {data['synthesis_id']} is not recorded for this run")
    raw = content.encode('utf-8')
    sha = hashlib.sha256(raw).hexdigest()
    imports = history(workspace)
    existing = next((i for i in imports if i.get('file_sha256') == sha), None)
    if existing:
        return existing, False
    from .study_editions import history as editions
    latest = (editions(workspace) or [None])[-1]
    (workspace / FOLDER).mkdir(exist_ok=True)
    (workspace / FOLDER / f'{sha}.json').write_bytes(raw)
    record = {'id': f'A{len(imports) + 1}', 'file_name': file_name, 'file_sha256': sha, 'imported_at': now(),
              'edition': {'number': latest['number'], 'sha256': latest['sha256']} if latest else None, **data}
    write(workspace / FILE, [*imports, record])
    return record, True


def _same(a, b):
    return _outcome_name(a or '') == _outcome_name(b or '')


def _number(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _mismatches(record, table, outcome_names):
    found, seen = [], {}
    for entry in record.get('inputs', []):
        identifier = entry.get('study_result_id')
        owner = {'study_result_id': identifier}
        if entry.get('study_id') in seen:
            found.append({'kind': 'duplicate_import_study', **owner, 'study_id': entry.get('study_id'),
                          'first': seen[entry['study_id']]})
        seen.setdefault(entry.get('study_id'), identifier)
        row = table.get(identifier)
        if not isinstance(row, dict):
            found.append({'kind': 'unknown_import_result', **owner})
            continue
        linked = row.get('outcome') if isinstance(row.get('outcome'), dict) else {}
        values = {'study_id': row.get('study_id'), 'outcome': linked.get('name') or outcome_names.get(linked.get('id')),
                  **{name: _text(row.get(name)) for name in ('unit', 'time_point', 'effect_measure', 'estimate')}}
        checks = (('study_id', entry.get('study_id') == values['study_id']),
                  ('outcome', _same(record.get('outcome'), values['outcome'])),
                  ('unit', values['unit'] is not None and _unit(record.get('unit')) == _unit(values['unit'])),
                  ('time_point', _same(record.get('time_point'), values['time_point'])),
                  ('effect_measure', _same(record.get('effect_measure'), values['effect_measure'])),
                  ('estimate', _number(values['estimate']) is not None and abs(_number(values['estimate']) - entry['estimate']) <= 1e-9))
        imported = {**record, **entry}
        found.extend({'kind': 'import_input_mismatch', **owner, 'field': name, 'imported': imported.get(name), 'table': values[name]}
                      for name, ok in checks if not ok)
    return found


def _changed(record, editions):
    from .study_editions import _without_passages
    edition = record.get('edition') or {}
    then = next((e for e in editions if e.get('sha256') == edition.get('sha256')), None)
    if then is None or not editions or editions[-1].get('sha256') == then.get('sha256'):
        return []
    latest = editions[-1]
    found = []
    for entry in record.get('inputs', []):
        identifier = entry.get('study_result_id')
        old, new = (then.get('results') or {}).get(identifier), (latest.get('results') or {}).get(identifier)
        if _without_passages(old) != _without_passages(new):
            fields = sorted(k for k in (old or {}).keys() | (new or {}).keys()
                            if _without_passages((old or {}).get(k)) != _without_passages((new or {}).get(k)))
            found.append({'study_result_id': identifier, 'fields': fields, 'imported_edition': then.get('number'),
                          'current_edition': latest.get('number')})
    return found


def check(record, editions, outcomes):
    """Whether the inputs match the latest study table edition, whether Canary reproduces the figures, and what changed
    since import."""
    from .synthesis import reproduce
    latest = editions[-1] if editions else {}
    table = latest.get('results') if isinstance(latest.get('results'), dict) else {}
    outcome_names = {o.get('id'): o.get('name') for o in _rows(outcomes)}
    mismatches = _mismatches(record, table, outcome_names)
    unverified = [{'field': 'standard_error', 'study_result_id': e.get('study_result_id'), 'imported': e.get('standard_error'),
                   'table': _text((table.get(e.get('study_result_id')) or {}).get('precision'))}
                  for e in record.get('inputs', []) if isinstance(table.get(e.get('study_result_id')), dict)]
    own = {e.get('study_result_id'): {'study_id': e.get('study_id')} for e in record.get('inputs', [])}
    reproduction = reproduce({'statistics': {'inputs': record.get('inputs', []), 'analyses': record.get('analyses', [])}}, own)
    return {'inputs_match': {'status': 'mismatched' if mismatches else 'unverified' if unverified else 'matched',
                             'mismatches': mismatches, 'unverified': unverified, 'edition': latest.get('number')},
            'reproduction': reproduction, 'stale': _changed(record, editions)}


def attach(bundle, graph):
    """Recheck the imports stored with a run against the study table editions in its answer."""
    from .methods_search import working_file
    located = working_file(Path(bundle), FILE)
    editions = graph.get('study_table_editions') if isinstance(graph.get('study_table_editions'), list) else []
    graph['analysis_imports'] = [dict(record, **check(record, editions, graph.get('protocol_outcomes')))
                                 for record in (history(located.parent) if located else [])]
    graph['binding_issues'] = [*(i for i in _rows(graph.get('binding_issues')) if not i.get('import_id')),
                               *(dict(i, section='syntheses') for i in issues(graph['analysis_imports']))]
    return graph


def issues(exported):
    """Each failed check of an import as a binding issue."""
    found = []
    for record in exported:
        owner = {'import_id': record.get('id'), 'synthesis_id': record.get('synthesis_id')}
        found.extend({**m, **owner} for m in record['inputs_match']['mismatches'])
        found.extend({'kind': 'import_not_reproduced', **owner, 'model': c['model'], **gap}
                     for c in record['reproduction'] for gap in c['differences'])
        found.extend({'kind': 'import_uses_changed_value', **owner, **change} for change in record['stale'])
    return found
