import json

from canary.methods_search import searches


def test_search_rounds_keep_overlaps_empty_results_and_interruptions(tmp_path):
    receipts = [dict(query_id='a', round=1, status=200, matched=4, retrieved=2, requested_at='2026-01-01',
                     ended='Reached the page limit for this round.', url='https://example.org/?api_key=secret'),
                dict(query_id='a', round=2, status=429, requested_at='2026-01-02'),
                dict(query_id='b', round=1, status=200, matched=0, retrieved=0),
                dict(query_id='c', round=1, status=200, matched=4, retrieved=2)]
    (tmp_path / 'openalex-requests.json').write_text(json.dumps(receipts))
    progress = dict(queries={key: dict(search=key, complete=key == 'b') for key in 'abc'})
    first, interrupted, empty, overlap = searches(tmp_path, progress)
    assert (first['matched'], first['retrieved'], overlap['retrieved']) == (4, 2, 2)
    assert interrupted['ended'] == 'Request failed (HTTP 429).'
    assert interrupted['last_search'] == '2026-01-02'
    assert empty['matched'] == empty['retrieved'] == 0
    assert 'secret' not in json.dumps(first)
