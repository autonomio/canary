"""Authentication CLI: HTTP client only; the service owns Codex and credentials."""
from __future__ import annotations

import json
import sys
import time
import webbrowser

import httpx


def run(args, port: int) -> int:
    login_id = None
    with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=35, trust_env=False) as client:
        def request(method, path, body=None):
            response = client.request(method, path, **({'json': body} if body is not None else {}))
            data = response.json()
            if response.is_error:
                raise RuntimeError(data.get('error', f'HTTP {response.status_code}'))
            return data

        try:
            if args.auth_cmd in ('status', 'logout'):
                data = request('GET', '/api/auth/status') if args.auth_cmd == 'status' else request('POST', '/api/auth/logout', {})
                if args.json:
                    print(json.dumps(data))
                else:
                    print('Codex: ' + data['state'].replace('_', ' ') +
                          (f" ({data['mode']})" if data['mode'] else ''))
                    if data['error']:
                        print(data['error'], file=sys.stderr)
                return 0 if data['available'] else 1
            data = request('POST', '/api/auth/login', {'method': 'device' if args.device else 'browser'})
            login_id = data['id']
            if data['state'] == 'pending':
                print(data['url'], flush=True)
                if data['user_code']:
                    print(f"Code: {data['user_code']}", flush=True)
                else:
                    webbrowser.open(data['url'])
                print('Complete sign-in in your browser. Ctrl-C cancels.', flush=True)
            while data['state'] == 'pending':
                time.sleep(1)
                data = request('GET', f'/api/auth/login/{login_id}')
            if data['state'] != 'complete':
                raise RuntimeError(data['error'] or f"Login {data['state']}.")
            status = request('GET', '/api/auth/status')
            if not status['authenticated']:
                raise RuntimeError(status['error'] or 'Login completed but no authenticated account is available.')
            print('Codex: signed in. Model access is checked when research starts.')
            return 0
        except KeyboardInterrupt:
            if login_id:
                try:
                    request('POST', f'/api/auth/login/{login_id}/cancel', {})
                except (httpx.HTTPError, RuntimeError, ValueError):
                    pass
            print('Stopped waiting for login.', file=sys.stderr)
            return 130
        except httpx.HTTPError:
            print(f'Cannot reach the service. Start it with: canary runs serve --port {port}', file=sys.stderr)
            return 1
        except (RuntimeError, ValueError) as error:
            print(f'canary: {error}', file=sys.stderr)
            return 1
