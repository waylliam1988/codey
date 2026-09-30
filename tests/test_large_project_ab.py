from __future__ import annotations

from unittest import mock

from tests.manual import large_project_ab


def test_large_project_arms_differ_by_project_map_only() -> None:
    """Both arms run the same kernel; the experiment variable is project_map."""
    import inspect

    source = inspect.getsource(large_project_ab.run_arm)
    assert "project_map" in source
    assert "codec" not in source
    assert "BaselineCodec" not in source


def test_large_project_benchmark_mutations_are_hard_disabled() -> None:
    outcome = large_project_ab._read_only_error()

    assert not outcome.ok
    assert not outcome.changed
    assert "read-only" in outcome.model_text


class _RecordingProvider:
    name = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.closed = False

    def new_chat(self) -> None:
        return None

    def send(self, prompt: str, timeout: float | None = None) -> str:
        self.prompts.append(prompt or "")
        return '{"tool": "done", "args": {"summary": "ok"}}'

    def close(self) -> None:
        self.closed = True


def test_large_project_run_arm_sends_expected_project_map() -> None:
    import tempfile
    from pathlib import Path

    from codey.agents.request import AgentRequest

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        (project / "a.py").write_text("x = 1\n", encoding="utf-8")
        case = large_project_ab.Case(name="c", project=project, task="t", expected=())
        seen: dict[str, str] = {}
        for arm in ("baseline", "current"):
            fake = _RecordingProvider()
            requests: list[AgentRequest] = []
            real_run = large_project_ab.run
            with (
                mock.patch.object(large_project_ab, "connect_provider", return_value=fake),
                mock.patch.object(large_project_ab, "run", wraps=real_run) as run,
            ):
                row = large_project_ab.run_arm(case, arm, "fake", 0, 2)
                requests.append(run.call_args.args[0])
            assert row["stop_reason"] == "done", row
            assert fake.closed is True, arm
            request = requests[0]
            seen[arm] = request.project_map
            if arm == "baseline":
                assert request.project_map == ""
                assert all("Project Map" not in prompt for prompt in fake.prompts), arm
            else:
                assert "Project Map" in request.project_map, request.project_map
                assert "a.py" in request.project_map, request.project_map
                assert any("Project Map" in prompt for prompt in fake.prompts), arm
                assert any("a.py" in prompt for prompt in fake.prompts), arm
        assert seen["baseline"] != seen["current"]
