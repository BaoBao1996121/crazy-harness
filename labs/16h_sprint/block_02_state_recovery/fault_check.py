from faulty_recovery import should_reuse_persisted_response


def test_response_is_reused_immediately_after_it_is_persisted():
    events = ["model.requested", "model.completed"]
    assert should_reuse_persisted_response(events) is True
