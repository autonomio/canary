"""One configurable HTTP origin for CLI and agent clients."""
import os
from urllib.parse import urlparse


def endpoint(port=8789):
    value = os.environ.get('CANARY_API_URL') or f'http://127.0.0.1:{port}'
    parsed = urlparse(value)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('CANARY_API_URL must be an HTTP(S) origin without credentials, query or path prefix')
    return value.rstrip('/')
