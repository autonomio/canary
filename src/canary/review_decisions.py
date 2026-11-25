from typing import Any

import jsonschema

from .coverage_protocol import arr, enum, obj


def schema() -> dict[str, Any]:
    """Describe the evidence, judgement and participants required for a decision."""
    text = {'type': 'string', 'pattern': r'\S'}
    person = obj({'kind': enum('person'), 'name': text, 'role': text, 'identity': enum('self_declared')})
    software = obj({'kind': enum('software'), 'name': text, 'model_version': text, 'prompt_reference': text})
    actor = {'type':'object', 'if':{'properties':{'kind':{'const':'software'}}},
             'then':software, 'else':person}
    empty = {'type': 'null'}
    optional_actor = {'type':['object','null'], 'if':{'type':'object'}, 'then':actor}
    optional_checker = {'type':['object','null'], 'if':{'type':'object'}, 'then':person}
    result = obj({'subject': text, 'judgement': text, 'status': enum('proposed', 'accepted', 'modified', 'rejected'),
                  'reason': text, 'evidence': dict(arr(text), minItems=1),
                  'proposed_by': optional_actor, 'decided_by': optional_actor, 'checked_by': optional_checker})
    result.update({'if': {'properties': {'status': {'const': 'proposed'}}},
                   'then': {'properties': {'proposed_by': actor, 'decided_by': empty}},
                   'else': {'properties': {'decided_by': actor}}})
    return result


def validate(decision: object) -> None:
    """Raise ValueError if a decision cannot be recorded under the shared schema."""
    error = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(schema()).iter_errors(decision))
    if error is not None:
        raise ValueError('Invalid review decision at ' + error.json_path + ': ' + error.message[:160])
