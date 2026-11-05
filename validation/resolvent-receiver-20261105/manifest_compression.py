"""Estimate how a bundle manifest carrying a supplied research state compresses, on prose-like text.

Usage: python validation/resolvent-receiver-20261105/manifest_compression.py REPOSITORY_ROOT

Text is a word-bigram chain over the repository's Markdown (real English prose), so it has
English-like statistics without exact long repeats. Shape follows a supplied research state:
claims with statements and evidence excerpts, stored twice (YAML source and resolved JSON).
"""
import base64
import json
import lzma
from pathlib import Path
import random
import re
import sys
import zlib

import yaml

root = Path(sys.argv[1])
words = []
for path in sorted(root.glob('**/*.md')):
    if '.venv' in path.parts or 'node_modules' in path.parts:
        continue
    words += re.findall(r"[A-Za-z][A-Za-z'-]*[.,;:]?|[0-9]+(?:\.[0-9]+)?", path.read_text(errors='ignore'))
chain = {}
for a, b in zip(words, words[1:]):
    chain.setdefault(a, []).append(b)
rng = random.Random(185)


def sentence(n):
    word = rng.choice(words)
    out = [word]
    for _ in range(n - 1):
        word = rng.choice(chain.get(word) or words)
        out.append(word)
    text = ' '.join(out)
    return text[0].upper() + text[1:].rstrip('.,;:') + '.'


def state(target):
    claims, size, n = [], 0, 0
    while size < target:
        claim = {'id': f'C-{n:05d}', 'statement': sentence(rng.randint(18, 40)), 'class': 'derived_inference',
                 'uncertainty': rng.choice(['low', 'medium', 'high']),
                 'evidence': [{'source': f'S-{n:05d}-{k}', 'excerpt': ' '.join(sentence(rng.randint(15, 35)) for _ in range(2))}
                              for k in range(2)],
                 'valid_time': {'status': 'unknown', 'intervals': [], 'notes': []}}
        claims.append(claim)
        size += len(json.dumps(claim))
        n += 1
    return {'provenance': {'repository': 'example/world', 'revision': 'synthetic-revision'}, 'claims': claims}


value = state(2_600_000)
source_yaml = yaml.safe_dump({'question': sentence(12), 'research_state': value}, sort_keys=False, allow_unicode=True)
manifest = {'specification': {'source_yaml': source_yaml, 'resolved': {'research_state': value}}}
raw = json.dumps(manifest, ensure_ascii=False).encode()
embedded = len(json.dumps(json.dumps(manifest, ensure_ascii=False), ensure_ascii=False).encode())
xz = len(base64.b64encode(lzma.compress(raw)))
deflate = len(base64.b64encode(zlib.compress(raw, 9)))
print(json.dumps({'state_json_bytes': len(json.dumps(value).encode()), 'manifest_bytes': len(raw),
                  'embedded_string_bytes': embedded, 'xz_base64_bytes': xz, 'deflate_base64_bytes': deflate,
                  'xz_saving_vs_embedded': round(1 - xz / embedded, 3),
                  'text_zlib_ratio': round(len(raw) / len(zlib.compress(raw, 9)), 2)}, indent=2))
