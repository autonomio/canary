"""Read a user declaration; all compilation, artifacts and execution belong to the API."""
import json
from pathlib import Path
import sys

import httpx


def run(args, port):
    try:
        source = Path(args.declaration).read_text(encoding='utf-8')
        path = '/api/runs/compile' if args.dry_run else '/api/runs'
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=30, trust_env=False) as client:
            response = client.post(path, json={'yaml': source})
        data = response.json()
        if response.is_error:
            print(data.get('error', f'HTTP {response.status_code}'), file=sys.stderr)
            return 1
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    except (OSError, UnicodeError) as error:
        print(f'Cannot read research declaration: {error}', file=sys.stderr)
    except httpx.HTTPError:
        print(f'Cannot reach the API. Start it with: canary runs serve --port {port}', file=sys.stderr)
    except ValueError:
        print('The API returned an invalid response.', file=sys.stderr)
    return 1


def fetch_bundle(args, port):
    from urllib.parse import quote
    suffix = '/exports/' + args.connector if args.connector else '/bundle'
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=30, trust_env=False) as client:
            response = client.get('/api/runs/' + quote(args.key, safe='') + suffix)
        value = response.json()
        if response.is_error:
            print(value.get('error', f'HTTP {response.status_code}'), file=sys.stderr)
            return 1
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (httpx.HTTPError, ValueError):
        print('Cannot read the research bundle from the API.', file=sys.stderr)
        return 1
