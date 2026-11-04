"""Hash-addressed scientific round records, readable after source cleanup."""
import hashlib
import json

KINDS = ('research', 'audit', 'correction')


def content(record):
    if record['kind'] == 'research' and isinstance(record.get('source_json'), str):
        if json.loads(record['source_json']) != record.get('investigation'):
            raise ValueError('Original investigation and retained JSON bytes disagree')
        return record['source_json']
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def inventory(history):
    return [{'round': r['round'], 'kind': r['kind'], 'sha256': hashlib.sha256(content(r).encode('utf-8')).hexdigest()}
        for r in history]


def read(payload, round_index, kind):
    if type(round_index) is not int or round_index < 0 or kind not in KINDS:
        raise ValueError('Expected a nonnegative round and research, audit or correction kind')
    records = [r for r in payload.get('claim_revisions', []) if r['round'] == round_index and r['kind'] == kind]
    if len(records) != 1:
        raise KeyError('Scientific revision artifact not recorded')
    body = content(records[0])
    sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
    saved = [r for r in payload.get('history_manifest', []) if r['round'] == round_index and r['kind'] == kind]
    if not saved:
        raise KeyError('Scientific revision digest was not recorded in this historical bundle')
    if len(saved) != 1 or saved[0]['sha256'] != sha:
        raise ValueError('Scientific revision changed since its recorded digest')
    return {'round': round_index, 'kind': kind, 'sha256': sha, 'content': body}
