"""CLI task choices, explicit grants and reuse reach the formal request."""
import pytest

from codey.app import cli
from codey.app.headless_runner import HeadlessResult


@pytest.mark.parametrize("intent", ["project", "review", "research", "hybrid", "chat", "planning_readonly", "auto"])
def test_cli_exposes_task_intents_and_preserves_selected_intent(tmp_path, monkeypatch, intent):
    requests = []
    def run(request, *, emit_jsonl):
        requests.append(request)
        return HeadlessResult(0, "r", "s", "done")
    monkeypatch.setattr("codey.app.headless_runner.run_headless", run)
    assert cli.main(["agent", "--project", str(tmp_path), "--intent", intent, "task"]) == 0
    assert requests[0].intent == intent


def test_cli_reuse_session_and_explicit_network_grant_reach_request(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setattr("codey.app.headless_runner.run_headless", lambda request, **_: requests.append(request) or HeadlessResult(0, "r", "s", "done"))
    assert cli.main(["agent", "--project", str(tmp_path), "--intent", "review", "--session-id", "s",
                     "--review-source-run-id", "run_" + "1" * 32, "--allow-web", "task"]) == 0
    assert requests[0].session_id == "s"
    assert requests[0].review_source_run_id == "run_" + "1" * 32
    assert requests[0].requested_capabilities == ("web.read",)


def test_conflicting_cli_modes_are_rejected_before_execution(tmp_path, monkeypatch):
    monkeypatch.setattr("codey.app.headless_runner.run_headless", lambda *_a, **_k: pytest.fail("must not execute"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["agent", "--project", str(tmp_path), "--auto", "--readonly", "task"])
    assert exc.value.code == 2


@pytest.mark.parametrize("options", [
    ["--continue"],
    ["--intent", "review", "--review-source-run-id", "run_" + "1" * 32],
    ["--intent", "research", "--session-id", "s", "--review-source-run-id", "run_" + "1" * 32],
])
def test_cli_rejects_continuation_or_reuse_without_applicable_session(tmp_path, monkeypatch, options):
    monkeypatch.setattr("codey.app.headless_runner.run_headless", lambda *_a, **_k: pytest.fail("must not execute"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["agent", "--project", str(tmp_path), *options, "task"])
    assert exc.value.code == 2


def test_human_cli_displays_real_review_event_text():
    assert cli._human_cli_line({"type": "review", "text": "Local self-review approved"}) == "[codey] Local self-review approved"
