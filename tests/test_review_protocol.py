from canary.review_protocol import outcome_definitions


def test_outcome_reference_identifies_the_measurement():
    force = dict(name='Breaking force', unit='N', time_point='24 hours', effect_measure='mean difference')
    first, = outcome_definitions([force])
    later, = outcome_definitions([dict(force, time_point='48 hours')])
    assert later['id'] != first['id']
    assert outcome_definitions([dict(reversed(force.items()))]) == [first]
    legacy, = outcome_definitions(['Breaking force'])
    assert legacy['unit'] is None and legacy['id'] != first['id']
