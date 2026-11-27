from canary.review_protocol import outcome_definitions


def test_outcome_reference_identifies_the_measurement():
    force = dict(name='Breaking force', unit='N', time_point='24 hours', effect_measure='mean difference')
    first, = outcome_definitions([force])
    later, = outcome_definitions([dict(force, time_point='48 hours')])
    assert later['id'] != first['id']
    assert outcome_definitions([dict(reversed(force.items()))]) == [first]
    legacy, = outcome_definitions(['Breaking force'])
    assert legacy['unit'] is None and legacy['id'] != first['id']


def test_amendment_preserves_both_plans_and_retries_once(tmp_path):
    import pytest
    from canary import review_protocol as protocol
    from canary.coverage_runtime import write
    write(tmp_path / 'run.json', {'protocol_version': 1})
    initial = dict(question='When?', outcomes=['At six weeks'])
    protocol.save_plan(tmp_path, initial)
    write(tmp_path / 'selection.json', {'decisions': [{'work_id': 'A'}]})
    updated = dict(initial, outcomes=['At six weeks', 'At six months'])
    with pytest.raises(ValueError, match='amendment_reason'):
        protocol.save_plan(tmp_path, updated)
    updated['amendment_reason'] = 'A six-month follow-up was reported during screening.'
    protocol.save_plan(tmp_path, updated)
    protocol.save_plan(tmp_path, updated)
    first, second = protocol.versions(tmp_path / protocol.FILE)
    assert first['plan'] == initial and second['previous'] == first['sha256']
    assert second['outcomes'][0] == first['outcomes'][0]
    assert second['stage'] == 'After screening began' and second['changed'] == ['outcomes']
    assert second['reason'] == updated['amendment_reason']
    second['plan']['outcomes'] = []
    write(tmp_path / protocol.FILE, [first, second])
    with pytest.raises(ValueError, match='has changed'):
        protocol.versions(tmp_path / protocol.FILE)


def test_export_returns_the_same_versions_before_and_after_compaction(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from canary import compact_bundle, dashboard, research_bundle, review_protocol, runs
    from test_compact_bundle import prepared

    prepared(tmp_path)
    review_protocol.save_plan(tmp_path, dict(question='When?', outcomes=['Six weeks']))
    expected = review_protocol.versions(tmp_path / review_protocol.FILE)
    handler = object.__new__(dashboard.Handler)
    handler.supervisor = SimpleNamespace(root=tmp_path.parent, registry=None)
    handler.dashboard_enabled = True
    monkeypatch.setattr(runs, 'get', lambda *args: {'key': 'review', 'bundle': str(tmp_path)})
    handler.path = '/api/runs/review/prisma/protocol'
    responses = []
    handler._send = lambda *args: responses.append(args)
    research_bundle.seal(tmp_path, state='running')
    handler.do_GET()
    research_bundle.seal(tmp_path)
    compact_bundle.retain(tmp_path)
    handler.do_GET()
    assert [status for status, _ in responses] == [200, 200]
    assert responses[0][1] == responses[1][1]
    assert responses[0][1]['versions'] == expected
    assert responses[0][1]['registration'] == {'status': 'unknown'}


def test_registration_keeps_its_external_date_and_source():
    import pytest
    from canary.research_spec import CompileError, compile_mapping
    record = dict(status='registered', registry='Registry', identifier='R1',
                  registered_on='2026-01-10', record_url='https://example.org/reviews/R1')
    assert compile_mapping({'question': 'When?', 'registration': record})['resolved']['registration'] == record
    record.pop('record_url')
    with pytest.raises(CompileError, match='registry record'):
        compile_mapping({'question': 'When?', 'registration': record})
    assert compile_mapping({'question': 'When?', 'registration': {'status': 'unregistered'}})['resolved'][
        'registration'] == {'status': 'unregistered'}


def test_amendment_lists_only_changes_to_the_previous_version(tmp_path):
    from canary import methods, review_protocol

    base = dict(question='When?', eligibility={'core': 'Adults'}, outcomes=['Strength'],
                queries=[dict(id='Q1', search='strength'), dict(id='Q2', search='yield')])
    review_protocol.save_plan(tmp_path, base)
    revised = dict(base, eligibility={'core': 'Adults and children'},
                   queries=[base['queries'][0], dict(id='Q3', search='tensile porosity')],
                   amendment_reason='The first search missed tensile tests.')
    review_protocol.save_plan(tmp_path, revised)
    report = methods.report({}, tmp_path)
    amendment = next(s for s in report['sections'] if s['title'] == 'Protocol amendment 1')
    changes = {row['label']: row['text'] for row in amendment['entries']}
    assert changes['Added search · Q3'] == 'tensile porosity'
    assert changes['Removed search · Q2'] == 'yield'
    assert changes['Previously · Eligibility · Core'] == 'Adults'
    assert changes['Now · Eligibility · Core'] == 'Adults and children'
    assert changes['Reason'] == revised['amendment_reason']
    assert 'strength' not in changes.values() and 'Strength' not in changes.values()
    assert 'Question' not in changes
    assert report['protocol_versions'][0]['plan'] == base


def test_protocol_byline_comes_from_the_declaration(tmp_path):
    from canary import coverage_runtime as runtime, methods, research_spec, review_protocol

    draft = dict(question='When?', outcomes=[])
    for index, author in enumerate([None, 'A. Rivera']):
        workspace = tmp_path / str(index)
        declaration = {'question': 'When?'}
        if author:
            declaration['protocol_author'] = author
        runtime.write(workspace / 'run-spec.json', research_spec.compile_mapping(declaration)['resolved'])
        review_protocol.save_plan(workspace, draft)
        report = methods.report({}, workspace)
        version, = report['protocol_versions']
        assert version['author'] == (author or 'Not recorded')
        bylines = [e['text'] for e in report['sections'][0]['entries'] if e['label'] == 'Protocol author']
        assert bylines == ([author] if author else [])


def test_resubmitting_queries_in_another_order_is_not_an_amendment(tmp_path):
    from canary import review_protocol
    from canary.coverage_runtime import write

    write(tmp_path / 'run.json', {'protocol_version': 1})
    plan = dict(question='When?', outcomes=[], queries=[dict(id='Q1', search='strength'), dict(id='Q2', search='yield')])
    review_protocol.save_plan(tmp_path, plan)
    review_protocol.save_plan(tmp_path, dict(plan, queries=plan['queries'][::-1]))
    assert len(review_protocol.versions(tmp_path / review_protocol.FILE)) == 1
