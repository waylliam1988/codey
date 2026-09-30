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


def test_large_project_run_arm_sends_expected_project_map() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        case = large_project_ab.Case(name="c", project=project, task="t", expected=())
        for arm, expect_empty in (("baseline", True), ("current", False)):
            with mock.patch.object(
                large_project_ab, "connect_provider", side_effect=AssertionError("no real provider")
            ):
                pass
            # Capture the AgentRequest by patching run() instead of connecting.
            with (
                mock.patch.object(large_project_ab, "connect_provider") as connect,
                mock.patch.object(large_project_ab, "run") as run,
            ):
                fake_raw = mock.Mock()
                fake_raw.name = "fake"
                fake_raw.send.return_value = '{"tool": "done", "args": {"summary": "ok"}}'
                connect.return_value = fake_raw
                run.return_value = mock.Mock(
                    stop_reason="done", changed=False, turns=1, summary="ok"
                )
                large_project_ab.run_arm(case, arm, "fake", 0, 2)
                request = run.call_args.args[0]
                if expect_empty:
                    assert request.project_map == ""
                else:
                    assert isinstance(request.project_map, str)
