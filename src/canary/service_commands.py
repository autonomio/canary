"""Compatibility workflows run only inside workers owned by the API."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import TypedDict

from . import cli, runs

COMMANDS = {'review','analyze','cycle','inspect','resume','assess','revise','publish'}


class ActionRequest(TypedDict):
    command: str
    options: dict


def command_argv(command, options):
    """Use the existing parser's names/types/choices; never accept an executable."""
    if not isinstance(command, str) or command not in COMMANDS or not isinstance(options, dict):
        raise ValueError('Unsupported compatibility command')
    parser = cli.argument_parser()
    group = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    selected = group.choices[command]
    actions = {a.dest:a for a in selected._actions if a.dest != 'help'}
    if set(options) - actions.keys():
        raise ValueError('Unknown command options: '+', '.join(sorted(set(options)-actions.keys())))
    argv = [command]
    for field, action in actions.items():
        value = options.get(field, action.default)
        if isinstance(action, argparse._StoreTrueAction):
            if type(value) is not bool: raise ValueError(field+' must be boolean')
            if value: argv.append(action.option_strings[0])
        elif value is not None:
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError(field+' must be a scalar')
            if action.option_strings: argv.append(action.option_strings[0])
            argv.append(str(value))
    try:
        selected.parse_args(argv[1:])
    except SystemExit:
        raise ValueError('Invalid compatibility command options') from None
    return argv


def prepare(command, options, root, registry):
    command_argv(command, options)
    options = dict(options)
    if options.get('csv'):
        value = options['csv']
        if not isinstance(value, str) or not value.startswith('input:'):
            raise ValueError('CSV data requires an uploaded input ID')
        identifier = value.removeprefix('input:')
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('Invalid input ID')
        source = root/'inputs'/identifier
        if not source.is_file(): raise ValueError('Unknown input ID')
        options['csv'] = str(source)
    for field in ('bundle', 'run_dir'):
        if options.get(field):
            row = runs.get(options[field], registry)
            source = Path(row['bundle'] if row else options[field]).expanduser().resolve()
            if not source.is_dir(): raise ValueError('Unknown run or server-side bundle directory')
            options[field] = str(source)
    command_argv(command, options)
    return options


def execute(packet, workspace):
    """Copy a referenced historical input before any legacy workflow can write it."""
    from .runtime_paths import data_root
    command = packet['command']
    options = prepare(command, packet['options'], data_root(), runs.REGISTRY)
    workspace = Path(workspace)
    if workspace.exists(): raise ValueError('A compatibility action requires new API-owned storage')
    source = options.get('bundle') or options.get('run_dir')
    if source:
        if workspace.resolve().is_relative_to(Path(source).resolve()):
            raise ValueError('The input directory cannot contain the new action workspace')
        shutil.copytree(source, workspace)
        if (workspace/'bundle.json').exists():
            (workspace/'bundle.json').rename(workspace/'source-bundle.json')
        for name in ('bundle','run_dir'):
            if options.get(name): options[name] = str(workspace)
    options['out'] = str(workspace) if command in ('review','analyze','cycle','assess','revise') else options.get('out')
    if command in ('resume',): options['fork'] = None
    options = {k:v for k,v in options.items() if v is not None}
    workspace.parent.mkdir(parents=True, exist_ok=True)
    output = workspace.parent/(workspace.name+'.action-result.json')
    try:
        code = cli.engine_main(command_argv(command, options))
        output.write_text(json.dumps({'command':command,'exit_code':code}))
        return code
    except Exception as error:
        output.write_text(json.dumps({'command':command,'error':str(error)}))
        raise


if __name__ == '__main__':
    packet = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    raise SystemExit(execute(packet, os.environ['OUT']))
