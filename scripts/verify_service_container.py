"""Service-image bootstrap and persistence proof; no provider inference."""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.STDOUT).strip()


def main():
    image = sys.argv[1]
    name = 'canary-service-proof-' + uuid.uuid4().hex[:10]
    data, auth = name + '-data', name + '-auth'
    try:
        for attempt in range(2):
            docker('run', '-d', '--name', name, '-p', '127.0.0.1::8789',
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
                          'provider_inference': False}))
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'volume', 'rm', data, auth], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
