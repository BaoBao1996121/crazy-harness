import subprocess

from work.check_course_ready import BUG_CARD_TIMEOUT_SECONDS, bug_card_expects_failure, run_check


def test_unfinished_bug_card_is_expected_to_start_red(tmp_path):
    assert bug_card_expects_failure(tmp_path) is True


def test_completed_bug_card_is_expected_to_stay_green(tmp_path):
    (tmp_path / "LEARNER_COMPLETED.md").write_text("completed", encoding="utf-8")

    assert bug_card_expects_failure(tmp_path) is False


def test_bug_card_timeout_allows_slow_windows_process_startup():
    assert BUG_CARD_TIMEOUT_SECONDS >= 60


def test_run_check_timeout_is_failure_even_when_red_is_expected(monkeypatch):
    def timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)

    result = run_check(
        "expected-red",
        ["python", "-m", "pytest", "fault_check.py"],
        expect_failure=True,
        timeout_seconds=1,
    )

    assert result.status == "failed"
    assert "timed out after 1s" in result.detail


def test_run_check_disables_unrelated_pytest_plugin_autoload(monkeypatch):
    captured: dict = {}

    def complete(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", complete)

    result = run_check("pytest", ["python", "-m", "pytest", "tests"])

    assert result.status == "passed"
    assert captured["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
