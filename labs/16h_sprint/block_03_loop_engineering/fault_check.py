from faulty_gate import can_submit


def test_missing_test_evidence_blocks_submission():
    output = {"risk_level": "low"}
    assert can_submit(output, evidence={"tests": []}, pending=[]) is False


def test_pending_operation_blocks_submission():
    output = {"risk_level": "low"}
    assert can_submit(output, evidence={"tests": ["event://ok"]}, pending=["op-1"]) is False
