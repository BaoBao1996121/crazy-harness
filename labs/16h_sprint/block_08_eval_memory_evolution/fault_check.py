from faulty_evolution import should_promote


def test_cheaper_but_less_successful_candidate_is_rejected():
    assert should_promote(0.95, 0.70, 1000, 700) is False
