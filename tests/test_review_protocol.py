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
    author = dict(kind='software', name='Researcher')
    initial = dict(question='When?', outcomes=['At six weeks'], draft_author=author)
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
