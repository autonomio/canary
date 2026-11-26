from canary import prisma


def test_catalogue_contains_each_official_item_once():
    main = ('1 2 3 4 5 6 7 8 9 10a 10b 11 12 13a 13b 13c 13d 13e 13f 14 15 '
            '16a 16b 17 18 19 20a 20b 20c 20d 21 22 23a 23b 23c 23d 24a 24b 24c 25 26 27').split()
    abstract = [f'abstract-{i}' for i in range(1, 13)]
    items = prisma.catalogue()['items']
    assert [item['id'] for item in items] == main + abstract
    assert all(item['title'] and all(item['elements']) and item['elements'] for item in items)
    page = prisma.html(prisma.report({}, dict(entries=[], last_entry='initial')))
    for heading in prisma.catalogue()['headings'].values():
        assert page.count(f'<th colspan="3">{heading}</th>') == 1
    assert items[1]['see_also'] == abstract
    assert [*prisma.catalogue()['headings']] == [str(i) for i in range(1, 28)] + ['abstract']
    assert [item['heading'] for item in items[:42]] == [n.rstrip('abcdef') for n in main]
    assert {item['heading'] for item in items[42:]} == {'abstract'}


def test_saved_question_links_to_the_actual_passage():
    view = {'answer': {'question': 'What changes tensile strength?'}}
    result = prisma.report(view, {'entries': [], 'last_entry': 'initial'})
    item = next(row for row in result['items'] if row['id'] == 'abstract-2')
    assert item['status'] == 'complete'
    answer, = item['elements']
    assert answer['passages'] == [dict(text=view['answer']['question'], evidence=['/answer/question'], supplier='run')]
    page = prisma.html(result)
    assert f'href="{answer["location"]}"' in page
    assert f'id="{answer["location"][1:]}"' in page
    assert '<blockquote>What changes tensile strength?</blockquote>' in page


def declaration(item, judgement, reason, ident='r1'):
    from test_review_history import decision, PERSON
    return dict(id=ident, replaces=None, decision=decision(reason, subject_id='prisma:'+item,
                judgement=judgement, decided_by=PERSON, evidence=['Signed methods note, section 2']))


def declared_item(number, *entries):
    result = prisma.report({}, dict(entries=list(entries), last_entry=entries[-1]['id'] if entries else 'initial'))
    return next(row for row in result['items'] if row['id'] == number)


def test_unregistered_missing_and_unrecorded_have_different_meanings():
    assert declared_item('24a')['status'] == 'missing'
    entry = declaration('24a:1', 'not_used', 'The authors did not register this review.')
    item = declared_item('24a', entry)
    assert item['status'] == 'complete'
    assert item['elements'][0]['passages'][0]['supplier'] == 'author'
    lost = declaration('24a:1', 'archive_missing', 'The register receipt was lost.')
    assert declared_item('24a', lost)['elements'][0]['basis'] == 'archive_missing'


def test_inapplicable_needs_a_reason_and_missing_elements_are_named():
    entry = declaration('abstract-7:2', 'inapplicable', 'The review tested polymer specimens. It enrolled no people.')
    records = [declaration('abstract-7:1', 'reported', 'Four studies.', 'a'), entry,
               declaration('abstract-7:3', 'reported', 'Each study tested tensile specimens.', 'c')]
    item = declared_item('abstract-7', *records)
    assert item['status'] == 'complete'
    assert item['elements'][1]['passages'][0]['text'] == entry['decision']['reason']
    partial = declared_item('abstract-7', *records[:-1])
    assert partial['status'] == 'partial'
    assert partial['missing_elements'] == ['Summarise relevant study characteristics.']
    assert declared_item('20d', declaration('20d:1', 'inapplicable', ' '))['status'] == 'missing'


def test_an_amended_or_disputed_declaration_is_not_counted_twice():
    old = declaration('24a:1', 'reported', 'Registered under protocol 123.')
    updated = declaration('24a:1', 'not_used', 'The authors did not register this review.', 'r2')
    assert declared_item('24a', old, updated)['status'] == 'partial'
    updated['replaces'] = old['id']
    assert declared_item('24a', old, updated)['status'] == 'complete'


def test_export_rejects_missing_duplicate_and_unknown_item_ids():
    import pytest
    result = prisma.report({}, dict(entries=[], last_entry='initial'))
    items = result['items']
    for invalid in [items[:-1], [*items[:-1], items[0]], [*items[:-1], dict(items[-1], id='unknown')]]:
        with pytest.raises(ValueError, match='checklist ID'):
            prisma.validate(dict(result, items=invalid))


def test_corrected_author_answer_keeps_its_subject_and_the_run_evidence():
    from test_review_history import decision, PERSON
    answer = decision('Original objective', subject_id='prisma:abstract-2:1', judgement='reported', decided_by=PERSON)
    correction = decision('Corrected objective', judgement='reported', decided_by=PERSON)
    entries = [dict(id='one', decision=answer), dict(id='two', replaces='one', subject_id='prisma:abstract-2:1',
                                                      decision=correction)]
    report = prisma.report({'answer': {'question': 'Original research question'}}, dict(entries=entries, last_entry='two'))
    row = next(row for row in report['items'] if row['id'] == 'abstract-2')
    assert row['status'] == 'complete'
    passages = row['elements'][0]['passages']
    assert [p['text'] for p in passages] == ['Original research question', 'Corrected objective']
    assert passages[1]['evidence'] == ['two', *correction['evidence']]


def test_abstract_counts_only_use_the_recorded_unit():
    view = {'coverage': dict(discovered_candidates=93, retrieved_sources=52, reviewed_papers=50,
                             related_report_groups={'group': ['r1', 'r2']}, participant_count=False)}
    result = prisma.report(view, dict(entries=[], last_entry='initial'))
    assert [c['value'] for c in result['counts']] == [93, None, 50, None, None]
    assert 'Reviewed publications: 50 · Included studies: unknown · Participants: unknown' in prisma.html(result)
    view['coverage'].update(included_report_count=48, included_study_count=40, participant_count=0)
    result = prisma.report(view, dict(entries=[], last_entry='initial'))
    assert [c['value'] for c in result['counts']] == [93, 48, 50, 40, 0]
    studies = next(row for row in result['items'] if row['id'] == 'abstract-7')
    assert [e['status'] for e in studies['elements']] == ['complete', 'complete', 'missing']


def test_abstract_shows_the_reason_participants_do_not_apply():
    entry = declaration('abstract-7:2', 'inapplicable', 'The experiments used polymer specimens.')
    result = prisma.report({}, dict(entries=[entry], last_entry=entry['id']))
    page = prisma.html(result)
    assert 'Participants: unknown' in page
    assert 'Participants · author statement: <a href="#decision-r1">The experiments used polymer specimens.' in page
    assert 'id="decision-r1" href="../review-decisions?last_entry=r1"' in page
    assert result['counts'][-1]['value'] is None


def test_selection_limitations_remain_linked_when_saved_as_a_list():
    view = {'paper_selection': {'selection': {'selection_limitations': ['Searches were capped.', 'Recall is unknown.']}}}
    result = prisma.report(view, dict(entries=[], last_entry='initial'))
    item = next(row for row in result['items'] if row['id'] == '23c')
    assert item['status'] == 'partial'
    passage, = item['elements'][0]['passages']
    assert passage['text'] == 'Searches were capped.\nRecall is unknown.'
    assert passage['evidence'] == ['/paper_selection/selection/selection_limitations']


def test_selection_summary_uses_saved_counts_and_keeps_original_wording():
    selection = dict(method='coverage', selected_for_review_ids=['x', 'x'], selection_limitations=['Original note.'])
    coverage = dict(discovered_candidates=10, assessed_candidates=3, available_papers=1,
                    search_progress={'queries': {'first': {'truncated': True}, 'second': {'complete': True}}})
    result = prisma.report(dict(paper_selection={'selection': selection}, coverage=coverage),
                           dict(entries=[], last_entry='initial'))
    assert result['selection_summary'] == ['Canary assessed 3 of the 10 records it found.',
                                           'The remaining 7 records may include relevant papers.']
    assert prisma.selection_summary({}) == []
    item = next(row for row in result['items'] if row['id'] == '23c')
    assert item['reader_question'] == 'What might the review itself have missed?'
    assert item['elements'][0]['passages'][0]['text'] == 'Original note.'
