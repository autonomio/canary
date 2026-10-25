"""Service-image bootstrap and persistence proof; no provider inference."""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


def docker(*args):
    result = subprocess.run(['docker', *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(result.stdout.strip())
    return result.stdout.strip()


def main():
    image = sys.argv[1]
    name = 'canary-service-proof-' + uuid.uuid4().hex[:10]
    data, auth = name + '-data', name + '-auth'
    try:
        for attempt in range(2):
            docker('run', '-d', '--name', name, '-p', '127.0.0.1::8789',
                   '--security-opt', 'seccomp=unconfined', '--security-opt', 'apparmor=unconfined',
                   '-v', data + ':/data', '-v', auth + ':/auth', image)
            address = docker('port', name, '8789/tcp').splitlines()[0]
            url = 'http://' + address
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urllib.request.urlopen(url + '/api/status', timeout=2) as response:
                        status = json.load(response)
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            assert status['codex']['available'], status
            assert status['pdf_extraction']['engine'] == 'pdftotext', status
            assert status['data_directory'] == '/data'
            with urllib.request.urlopen(url + '/') as response:
                assert b'Research' in response.read()
            assert 'signed_out' in docker('exec', name, 'canary', 'auth', 'status', '--json')
            if attempt == 0:
                # Exercise the installed CLI parser with the production invocation.
                # Invalid JSON must stop before authentication or inference.
                docker('exec', name, 'python', '-c', """
import subprocess, tempfile
from pathlib import Path
from canary.coverage_runtime import command_line
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    shape = root / 'invalid.json'
    shape.write_text('{')
    result = subprocess.run(command_line('codex', 'gpt-6.1-sol', 'xhigh', root,
                                         shape, root / 'output.json'),
                            input='Invalid schema parser probe.', text=True,
                            capture_output=True, cwd=root, timeout=20)
    assert result.returncode != 0, result.stdout
    assert 'is not valid JSON' in result.stderr, result.stderr
""")
                docker('exec', name, 'codex', 'sandbox', '-c', 'sandbox_mode="workspace-write"',
                       '-c', 'sandbox_workspace_write.network_access=true', '--', 'python', '-c',
                       "import json,urllib.request; from pathlib import Path; "
                       "assert json.load(urllib.request.urlopen('http://127.0.0.1:8789/api/status'))['service']=='available'; "
                       "Path('/data/native-sandbox-proof').write_text('executed')")
            if attempt == 0:
                docker('exec', name, 'python', '-c',
                       "from pathlib import Path; assert not Path('/build').exists(); "
                       "Path('/data/persistence-proof').write_text('research-marker'); "
                       "Path('/auth/persistence-proof').write_text('auth-marker')")
            else:
                docker('exec', name, 'python', '-c',
                       "from pathlib import Path; assert Path('/data/persistence-proof').read_text()=='research-marker'; "
                       "assert Path('/auth/persistence-proof').read_text()=='auth-marker'")
            docker('rm', '-f', name)
        print(json.dumps({'image': image, 'api': True, 'dashboard': True, 'codex': True,
                          'pdftotext': True, 'persistent_data_and_auth_roots': True,
                          'native_invocation_parser': True,
                          'native_sandbox_execution_and_api': True,
                          'provider_inference': False}))
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'volume', 'rm', data, auth], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
