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


def test_evaluation_matches_the_recorded_session(tmp_path):
    from canary.methods_automation import sessions
    (tmp_path / 'research-prompt.txt').write_text('Screen the abstracts.')
    (tmp_path / 'research-receipt.json').write_text(json.dumps(dict(cli_version='1.2', model='m', reasoning_effort='high')))
    session, = sessions(tmp_path)
    assert session['evaluation'] is None
    config = session['configuration']
    (tmp_path / 'methods-evaluations.json').write_text(json.dumps([
        dict(configuration=dict(config, effort='low'), url='https://example.org/other'),
        dict(configuration=config, url='https://example.org/evaluation')]))
    session, = sessions(tmp_path)
    assert session['evaluation'] == 'https://example.org/evaluation'
    assert session['prompt'] == 'Screen the abstracts.'


def test_partial_archive_shows_unknowns_and_only_attributes_recorded_decisions(tmp_path):
    from canary import methods
    graph = {'evidence': None, 'claims': [{'evidence_ids': None}], 'audit': {'status': 'not_run'}}
    decisions = [dict(work_id='A', decision='exclude', reason=None, decided_by='software_only'),
                 dict(work_id='B', decision='exclude', reason='Out of scope')]
    view = dict(answer=graph, paper_selection={'selection': {'decisions': decisions}}, access=None,
                coverage=dict(discovered_candidates=3, assessed_candidates=2),
                query_net={'queries': [dict(id='Q1', search='water quality')]})
    report = methods.report(view, tmp_path)
    entries = {r['label']: r for s in report['sections'] for r in s['entries']}
    assert 'Software made this decision alone.' in entries['A · exclude']['text']
    assert 'Software' not in entries['B · exclude']['text']
    assert entries['Records left unscreened']['text'] == '1'
    assert 'not_run' in entries['Who checked the findings']['text']
    assert report['searches'][0]['attempted'] is False


def test_incomplete_page_preserves_known_retrievals(tmp_path):
    receipts = [dict(query_id='a', round=1, status=200, retrieved=3, matched=40),
                dict(query_id='a', round=1, status=200),
                dict(query_id='a', round=2, status=429)]
    (tmp_path / 'openalex-requests.json').write_text(json.dumps(receipts))
    first, second = searches(tmp_path, dict(queries={'a': dict(search='lake', matched=40)}))
    assert first['retrieved'] == 3 and first['retrieved_partial']
    assert second['matched'] is None and second['retrieved'] == 0


def test_interrupted_search_without_a_published_projection(tmp_path):
    from canary import methods
    (tmp_path / 'question-plan.json').write_text(json.dumps({'question': 'Lake water', 'queries': [
        {'id': 'a', 'search': 'water'}]}))
    (tmp_path / 'search-progress.json').write_text(json.dumps({'queries': {'a': {'search': 'water', 'failure_status': 429}}}))
    (tmp_path / 'openalex-requests.json').write_text(json.dumps([dict(query_id='a', status=429, round=1)]))
    report = methods.report({}, tmp_path)
    assert report['question'] == 'Lake water'
    assert report['searches'][0]['ended'] == 'Request failed (HTTP 429).'


def test_interrupted_receipt_write_keeps_readable_pages(tmp_path):
    from canary import methods
    receipt = dict(query_id='a', round=1, requested_at='2026-01-01', status=200, matched=5, retrieved=2)
    (tmp_path / 'additional-source-requests.jsonl').write_text(json.dumps(receipt) + '\n{"page":')
    report = methods.report(dict(query_net={'queries': [dict(id='a', source='clinicaltrials', search='water')]}), tmp_path)
    assert report['searches'][0]['retrieved'] == 2
    assert any(row['label'] == 'Incomplete request log' for row in report['sections'][1]['entries'])
