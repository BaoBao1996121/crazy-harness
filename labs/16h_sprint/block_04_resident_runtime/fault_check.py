from faulty_recovery import recovery_action


def test_unknown_external_effect_must_reconcile_before_retry():
    assert recovery_action("unknown") == "reconcile"
