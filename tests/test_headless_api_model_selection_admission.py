"""Headless API model choices use the same durable admission as desktop."""
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.runtime.core.api_selection import ApiRunSelection


def test_headless_captures_explicit_model_before_submission(tmp_path):
    selection = ApiRunSelection("zen", "revision", "fixture", "openai-responses", True)
    request = HeadlessRequest(project=None, task="hello", provider_id="zen", intent="chat", state_home=tmp_path,
                              model_selection={"model": "fixture", "effort": "low"})
    seen = []
    with patch("codey.providers.api_connections.capture_selection", return_value=selection) as capture, \
            patch("codey.app.headless_runner.run_task_submission", side_effect=lambda deps, submission: seen.append(submission)):
        run_headless(request, emit_jsonl=lambda row: None)
    capture.assert_called_once_with("zen", {"model": "fixture", "effort": "low"})
    assert seen[0].model_selection == selection.to_payload()


def test_cli_forwards_api_model_and_effort(tmp_path):
    from codey.app.cli import main
    from codey.app.headless_runner import HeadlessResult

    with patch("codey.app.headless_runner.run_headless", return_value=HeadlessResult(0, "r", "s", "done")) as run:
        assert main(["agent", "--provider", "zen", "--model", "fixture", "--effort", "low", "--state-home", str(tmp_path), "hello"]) == 0
    assert run.call_args.args[0].model_selection == {"model": "fixture", "effort": "low"}
