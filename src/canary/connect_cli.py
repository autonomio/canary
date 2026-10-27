"""Local service bootstrap, followed exclusively by API-owned authentication."""
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx

from .api_client import endpoint
from .runtime_paths import data_root


def request(client, path):
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def start_local_service(client, origin):
    parsed = urlparse(origin)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1'):
        raise ValueError('Cannot reach the remote Canary API. Start Canary and install Codex on the service host; a laptop CLI is not a remote worker.')
    if not shutil.which('codex'):
        raise ValueError('Codex CLI is missing. Install it on the Canary host, then run canary connect again.')
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    log = root / 'service.log'
    with log.open('a') as output:
        process = subprocess.Popen([sys.executable, '-m', 'canary.cli', 'runs', 'serve',
                                    '--port', str(parsed.port or 80)], cwd=root,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    deadline = time.monotonic() + 15
    while True:
        try:
            status = request(client, '/api/status')
            if status.get('service') != 'available':
                raise ValueError('The configured endpoint is not a Canary service.')
            return
        except httpx.ConnectError:
            if process.poll() is not None or time.monotonic() >= deadline:
                raise ValueError('Canary did not start. See ' + str(log)) from None
            time.sleep(.1)  # Wait only for the service we just launched to bind.


def run(args, port):
    from . import auth_cli
    try:
        origin = endpoint(port)
        with httpx.Client(base_url=origin, timeout=35, trust_env=False) as client:
            try:
                receipt = request(client, '/api/connection')
            except httpx.ConnectError:
                start_local_service(client, origin)
                receipt = request(client, '/api/connection')
            if not receipt['auth']['available']:
                raise ValueError(receipt['auth']['error'] or 'Codex is unavailable on the service host.')
            if not receipt['auth']['authenticated']:
                code = auth_cli.run(SimpleNamespace(auth_cmd='login', device=args.device), port)
                if code:
                    return code
                receipt = request(client, '/api/connection')
            if not receipt['auth']['authenticated']:
                raise ValueError('Codex sign-in could not be confirmed by the service.')
            print(f"Connected to Canary at {origin}")
            print(f"Codex: {receipt['cli_version'] or 'version unavailable'} · {receipt['auth']['mode']} · {receipt['credential_store']} credentials")
            print('Execution: Canary service host · one shared account')
            print(receipt['model_access'])
            return 0
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404 and error.request.url.path == '/api/connection':
            print('Canary connection failed: this service predates canary connect. Update and restart the service when its active runs are finished.', file=sys.stderr)
        else:
            print('Canary connection failed: ' + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, httpx.HTTPError) as error:
        print('Canary connection failed: ' + str(error), file=sys.stderr)
        return 1
