from crazy_harness.cli import build_parser


def test_learning_cli_defaults_to_mock_and_can_select_team():
    args = build_parser().parse_args(["run", "dev-release", "--team"])

    assert args.mode == "mock"
    assert args.team is True
