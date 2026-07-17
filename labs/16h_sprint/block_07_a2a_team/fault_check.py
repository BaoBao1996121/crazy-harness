from faulty_peer_policy import authorize_peer


def test_second_hop_peer_request_is_denied():
    assert authorize_peer(2, 1, {"repository_evidence"}, {"repository_evidence"}) is False
