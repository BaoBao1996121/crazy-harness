from crazy_harness.worlds.cicd.agents import release_team_cards


def test_release_team_has_four_agents() -> None:
    cards = release_team_cards()
    assert [card.agent_id for card in cards] == ["coordinator", "scout", "builder", "reviewer"]
