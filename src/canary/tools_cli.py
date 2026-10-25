"""HTTP client for the native scientist's deterministic, run-scoped tools."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

import httpx


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--port', type=int, default=int(os.environ.get('CANARY_RUNS_PORT', '8789')))
    commands = parser.add_subparsers(dest='action', required=True)
    p = commands.add_parser('discover');p.add_argument('--plan', required=True);p.add_argument('--pages', type=int, default=5)
    p = commands.add_parser('acquire');p.add_argument('--ids', nargs='+', required=True)
    p.add_argument('--workers', type=int, default=12);p.add_argument('--retry-failed', action='store_true')
    commands.add_parser('catalog')
    p = commands.add_parser('source');p.add_argument('reference_id');p.add_argument('--match');p.add_argument('--page', type=int)
    p.add_argument('--context', type=int, default=2);p.add_argument('--limit', type=int, default=8)
    p = commands.add_parser('finalize');p.add_argument('--investigation', default='investigation.json')
    args = parser.parse_args(argv)
    body = {k:v for k,v in vars(args).items() if k not in ('action','run','port')}
    try:
        for name in ('plan', 'investigation'):
            if name in body:
                body[name] = json.loads(Path(body[name]).read_text(encoding='utf-8'))
        with httpx.Client(base_url=f'http://127.0.0.1:{args.port}', timeout=None, trust_env=False) as client:
            response = client.post('/api/runs/'+quote(args.run, safe='')+'/tools/'+args.action, json=body)
        result = response.json()
        if response.is_error:
            print(result.get('error', 'Tool failed'), file=sys.stderr);return 1
        print(json.dumps(result, ensure_ascii=False, indent=2));return 0
    except (OSError, ValueError, httpx.HTTPError) as error:
        print('Research tool failed: '+str(error), file=sys.stderr);return 1


if __name__ == '__main__':
    raise SystemExit(main())
