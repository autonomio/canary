"""Measure delivered contributions only; never access research working files."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time

from canary import bundle_encoding as codec


def measure(path):
    incoming = json.loads(Path(path).read_bytes())
    plain = codec.expand(incoming)
    original = codec.dumps(plain).encode('utf-8')
    encoding_cpu, decoding_cpu = [], []
    for _ in range(3):
        started = time.process_time()
        encoded = codec.encode(plain)
        encoding_cpu.append(time.process_time()-started)
        started = time.process_time()
        decoded = codec.expand(encoded)
        decoding_cpu.append(time.process_time()-started)
        assert decoded == plain
        for field in ('claims', 'evidence', 'sources'):
            assert encoded[field] == plain[field]
    body = codec.dumps(encoded).encode('utf-8')
    browser = subprocess.run(['node', '-e',
        "let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',x=>s+=x);"
        "process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(s))));"],
        input=body, capture_output=True, check=True, timeout=30)
    assert codec.expand(json.loads(browser.stdout)) == plain
    canonical = lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    assert hashlib.sha256(canonical(decoded)).digest() == hashlib.sha256(canonical(plain)).digest()
    return {'expanded_bytes': len(original), 'encoded_bytes': len(body),
        'saved_percent': round(100*(1-len(body)/len(original)), 2),
        'encode_cpu_ms_median': round(statistics.median(encoding_cpu)*1000, 2),
        'expand_cpu_ms_median': round(statistics.median(decoding_cpu)*1000, 2),
        'exact_value_roundtrip': True, 'embedded_artifact_strings_unchanged': True,
        'browser_json_roundtrip': True,
        'native_claims_evidence_sources_unchanged': True,
        'canonical_expanded_sha256': hashlib.sha256(canonical(plain)).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contributions', nargs='+', help='JSON responses already obtained through the API')
    args = parser.parse_args()
    print(json.dumps({'scope': 'Offline packaging only; no model calls or full-question timing claim.',
                      'samples': [measure(path) for path in args.contributions]}, indent=2))
