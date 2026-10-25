"""Application CLI commands are HTTP clients; file reads are explicit input uploads."""
import json
from pathlib import Path
import sys
from urllib.parse import quote

import httpx

from .api_client import endpoint


def run(args, port):
    def request(client, method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        value = response.json()
        if response.is_error:
            raise ValueError(value.get('error', f'HTTP {response.status_code}'))
        return value
    try:
        with httpx.Client(base_url=endpoint(port), timeout=30, trust_env=False) as client:
            if args.cmd == 'api':
                value = request(client,'GET','/openapi.json')
            elif args.cmd == 'ask':
                if args.out != './out/question':
                    raise ValueError('--out is replaced by API-owned storage; retrieve the returned run with canary bundle RUN_KEY')
                value = request(client, 'POST', '/api/runs', json={'spec': {'question': args.question,
                    'retrieval': {'target_papers': args.minimum_papers}, 'audit': {'budget_seconds': args.audit_seconds}}})
            elif args.cmd == 'runs':
                verb = args.runs_cmd
                key = quote(getattr(args, 'key', ''), safe='')
                if verb in ('list', 'status', 'show', 'log'):
                    path = {'list':'/api/runs', 'status':'/api/status', 'show':'/api/runs/'+key,
                            'log':'/api/runs/'+key+'/log'}[verb]
                    params = {'tail': args.tail} if verb == 'log' else None
                    value = request(client, 'GET', path, params=params)
                elif verb == 'adopt':
                    value = request(client, 'POST', '/api/runs/import', json={
                        'server_path': args.bundle, 'name': args.name, 'brief': args.brief})
                elif verb in ('footprint','discover'):
                    value = request(client,'GET','/api/footprint') if verb=='footprint' else request(client,'POST','/api/footprint/refresh',json={})
                elif verb == 'start':
                    from .runs import ambient_launch_env
                    argv = list(args.argv)
                    if argv and argv[0] == '--': argv.pop(0)
                    value = request(client, 'POST', '/api/runs', json={'name':args.name,'brief':args.brief,'argv':argv,'env':ambient_launch_env()})
                else:
                    body = {'name':args.name,'brief':args.brief} if verb == 'rerun' else {}
                    value = request(client, 'POST', '/api/runs/'+key+'/'+verb, json=body)
            else:
                options = {k:v for k,v in vars(args).items() if k not in ('cmd','port')}
                if options.get('csv'):
                    source = Path(options['csv'])
                    with source.open('rb') as stream:
                        uploaded = request(client, 'POST', '/api/inputs', content=stream,
                            headers={'Content-Type':'application/octet-stream','Content-Length':str(source.stat().st_size)})
                    options['csv'] = 'input:' + uploaded['id']
                value = request(client, 'POST', '/api/actions', json={'command':args.cmd,'options':options})
        if args.cmd == 'runs' and args.runs_cmd == 'list':
            print(f"{'KEY':28} {'STATUS':12} {'KIND':8} NAME")
            for row in value['runs']:
                print(f"{row.get('key','?'):28} {row.get('status','?'):12} {row.get('kind','?'):8} {row.get('name','')}")
        elif args.cmd == 'runs' and args.runs_cmd == 'log':
            if args.stream in ('all','timeline'):
                for ev in value.get('timeline', []):
                    print(f"{ev['t']} s{ev['seq']} [{ev['source']}:{ev['phase']}/{ev['event']}] {ev['detail']}")
            if args.stream in ('all','console'):
                console = value.get('console', {})
                print('--- console ---' if console.get('present') else '--- console: '+console.get('note','unavailable'))
                print('\n'.join(console.get('lines', [])))
        elif args.cmd == 'runs' and args.runs_cmd == 'adopt':
            print(f"adopted {value['key']}: {value['name']} [{value['status']}]")
        else:
            print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, httpx.HTTPError) as error:
        print('Canary API command failed: '+str(error), file=sys.stderr);return 1
