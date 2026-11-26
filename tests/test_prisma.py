from canary import prisma
from test_review_decisions_api import review_client


def test_catalogue_contains_each_official_item_once():
    main = ('1 2 3 4 5 6 7 8 9 10a 10b 11 12 13a 13b 13c 13d 13e 13f 14 15 '
            '16a 16b 17 18 19 20a 20b 20c 20d 21 22 23a 23b 23c 23d 24a 24b 24c 25 26 27').split()
    abstract = [f'abstract-{i}' for i in range(1, 13)]
    items = prisma.catalogue()['items']
    assert [item['id'] for item in items] == main + abstract
    assert all(item['title'] and all(item['elements']) and item['elements'] for item in items)
    assert items[1]['see_also'] == abstract


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


def test_saved_checklist_is_available_to_dashboard_cli_and_agent(tmp_path, monkeypatch, capsys, review_client):
    import json
    import httpx
    import jsonschema
    from canary import api_contract, cli, mcp_server, runs
    from test_compact_bundle import prepared

    client, registry, _, dispatch = review_client
    workspace = tmp_path / 'review'
    workspace.mkdir()
    prepared(workspace)
    runs.adopt_bundle(workspace, path=registry)
    response = client.get('/api/runs/review/prisma/checklist')
    assert response.status_code == 200
    report = response.json()
    assert len(report['items']) == 54
    assert client.get('/api/runs/review/prisma/report').status_code == 200
    contract = api_contract.document()
    tool = mcp_server.tool_catalog(contract)['canary_prisma_checklist']
    jsonschema.validate(report, tool['tool'].outputSchema)
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(dispatch)))
    assert cli.main(['checklist', 'review']) == 0
    assert json.loads(capsys.readouterr().out) == report


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
