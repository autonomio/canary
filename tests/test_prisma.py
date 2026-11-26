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
