"""Exercise the built wheel from outside the checkout, without provider inference."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def main():
    wheel = Path(sys.argv[1]).resolve()
    checkout = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='canary-installed-') as directory:
        base = Path(directory).resolve()
        env = dict(os.environ, CANARY_DATA_DIR=str(base / 'data'), CODEX_HOME=str(base / 'auth'))
        env.pop('PYTHONPATH', None)
        env.pop('CANARY_QUESTION_ENGINE_ROOT', None)
        env.pop('CANARY_RUNS_REGISTRY', None)
        subprocess.run(['uv', 'venv', '--python', sys.executable, str(base / 'venv')], check=True)
        python = str(base / 'venv/bin/python')
        requirements = base / 'runtime.txt'
        subprocess.run(['uv', 'export', '--frozen', '--no-dev', '--no-emit-project', '-o', str(requirements)],
                       cwd=checkout, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['uv', 'pip', 'install', '--python', python, '--require-hashes', '-r', str(requirements)], check=True)
        subprocess.run(['uv', 'pip', 'install', '--python', python, '--no-deps', str(wheel)], check=True)
        canary = str(base / 'venv/bin/canary')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        with (base / 'service.log').open('w+') as log:
            process = subprocess.Popen([canary, 'runs', 'serve', '--port', str(port)], cwd=base,
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                url = f'http://127.0.0.1:{port}'
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        log.seek(0)
                        raise AssertionError(log.read())
                    try:
                        with urllib.request.urlopen(url + '/api/runs', timeout=1) as response:
                            assert json.load(response)['runs'] == []
                        break
                    except urllib.error.URLError:
                        if time.monotonic() >= deadline:
                            log.flush(); log.seek(0)
                            print(log.read(), file=sys.stderr)
                            raise
                        time.sleep(.1)
                for route in ('/', '/assets/research.css', '/assets/InterVariable.woff2',
                              '/assets/Finlandica-Regular.woff2', '/assets/Finlandica-LICENSE.txt'):
                    with urllib.request.urlopen(url + route) as response:
                        assert response.read(), route
                status = json.loads(subprocess.check_output([canary, 'runs', 'status', '--port', str(port)], cwd=base, env=env))
                assert status['data_directory'] == str(base / 'data')
                request = urllib.request.Request(url + '/api/runs/compile',
                    data=json.dumps({'spec': {'question': 'Installed wheel verification?'}}).encode(),
                    headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request) as response:
                    assert json.load(response)['resolved']['research']['effort'] == 'xhigh'
                output = subprocess.check_output([python, '-m', 'canary.coverage_runtime', 'catalog',
                                                  '--workspace', str(base / 'empty')], cwd=base, env=env)
                assert json.loads(output) == []
                subprocess.run([python, '-c', 'from canary.runtime_paths import resource_root; '
                    'r=resource_root(); assert (r/"scripts/coverage_run.sh").is_file(); '
                    'assert (r/"uv.lock").is_file()'], cwd=base, env=env, check=True)
                print(json.dumps({'wheel': wheel.name, 'outside_checkout': True,
                                  'api': True, 'assets': True, 'compile': True, 'worker_tools': True}))
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()
