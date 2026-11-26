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


def test_damaged_log_only_limits_counts_from_that_log(tmp_path):
    receipt = dict(query_id='a', source='clinicaltrials', search='water', round=1, status=200, retrieved=2)
    (tmp_path / 'additional-source-requests.jsonl').write_text(json.dumps(receipt) + '\n{"page":')
    (tmp_path / 'openalex-requests.json').write_text(json.dumps([dict(query_id='b', status=200, retrieved=4)]))
    other, damaged = searches(tmp_path, {})
    assert (other['source'], other['retrieved'], other['retrieved_partial']) == ('openalex', 4, False)
    assert (damaged['source'], damaged['search'], damaged['retrieved_partial']) == ('clinicaltrials', 'water', True)


def test_unreadable_working_files_leave_the_rest_of_the_appendix_available(tmp_path):
    from canary import methods
    for name in ('selection.json', 'search-progress.json', 'question-plan.json'):
        (tmp_path / name).write_bytes(b'{')
    result = methods.report(dict(seed='Review question'), tmp_path)
    assert result['question'] == 'Review question'
    assert {e['label'] for e in result['sections'][0]['entries']} == {
        'selection.json', 'search-progress.json', 'question-plan.json'}


def test_single_prompt_evaluation_does_not_cover_follow_up_instructions(tmp_path):
    from canary.methods_automation import sessions
    receipt = dict(cli_version='1', model='m', reasoning_effort='high')
    path = tmp_path / 'correction-receipt.json'
    path.write_text(json.dumps(receipt))
    (tmp_path / 'correction-prompt.txt').write_text('Check the correction.')
    config = sessions(tmp_path)[0]['configuration']
    (tmp_path / 'methods-evaluations.json').write_text(json.dumps([
        dict(configuration=config, url='https://example.org/evaluation')]))
    for extra in [dict(response_retry={'resumed': True}), dict(capacity_pauses=[{'resumed': True}])]:
        path.write_text(json.dumps(dict(receipt, **extra)))
        assert sessions(tmp_path)[0]['evaluation'] is None


def test_counts_stay_with_their_source_when_logs_share_a_query_id(tmp_path):
    receipt = dict(query_id='a', status=200, round=1, retrieved=2)
    (tmp_path / 'openalex-requests.json').write_text(json.dumps([receipt, None]))
    (tmp_path / 'additional-source-requests.jsonl').write_text(json.dumps(dict(receipt, source='pubmed')) + '\n{')
    rows = searches(tmp_path, dict(queries={'b': dict(source='pubmed', returned=3),
                                           'a': dict(source='openalex', search='lake', complete=True)}))
    assert [(row['source'], row['retrieved'], row['retrieved_partial']) for row in rows] == [
        ('pubmed', 3, False), ('openalex', 2, True), ('pubmed', 2, True)]
    fallback, openalex, pubmed = rows
    assert fallback['attempted']
    assert openalex['search'] == 'lake' and openalex['ended'] == 'The source returned its final page.'
    assert pubmed['ended'] is None and pubmed['search'] == 'Search terms were not recorded.'


def test_full_paper_review_includes_the_reason_and_distinct_note():
    from canary.methods import screening_entries
    row = dict(work_id='p', eligibility='excluded', reason='Only a preview was available.')
    for note in (None, row['reason'], 'Only page one was visible.'):
        row['substantive_review_note'] = note
        review = screening_entries({'paper_selection': {'reviewed_papers': [row]}}, {}, {})[0]
        assert review['text'].splitlines() == ['excluded', row['reason']] + (
            [note] if note and note != row['reason'] else [])


def test_nested_session_uses_its_own_prompt_and_defers_to_root_receipt(tmp_path):
    from canary.methods_automation import sessions
    folder = tmp_path / 'research'
    folder.mkdir()
    receipt = 'research-receipt.json'
    prompt = 'research-prompt.txt'
    (folder / receipt).write_text('{"model":"nested"}')
    (folder / prompt).write_text('Nested instructions')
    (tmp_path / prompt).write_text('Root instructions')
    session, = sessions(tmp_path)
    assert (session['configuration']['model'], session['prompt'], session['round']) == (
        'nested', 'Nested instructions', None)
    (tmp_path / receipt).write_text('{"model":"root"}')
    session, = sessions(tmp_path)
    assert (session['configuration']['model'], session['prompt']) == ('root', 'Root instructions')
