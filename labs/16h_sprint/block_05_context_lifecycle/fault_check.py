from faulty_microcompact import next_hydration_turns


def test_one_turn_hydration_lease_expires():
    assert next_hydration_turns(1) == 0
