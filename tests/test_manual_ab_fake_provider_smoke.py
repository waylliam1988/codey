"""Manual A/B arms run the real kernel with a fake provider (no browser).

Locks that the three manual entry points execute end to end on a temporary
project: they send at least one model prompt, return a result, and close
the provider. No real browser, no real project reads.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock


class FakeProvider:
    name = "fake"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []
        self.closed = False

    def new_chat(self) -> None:
        return None

    def send(self, prompt: str, timeout: float | None = None) -> str:
        self.prompts.append(prompt or "")
        if self._replies:
            return self._replies.pop(0)
        return '{"tool": "done", "args": {"summary": "done summary"}}'

    def close(self) -> None:
        self.closed = True


def _make_case(tmp_path: Path, name: str = "smoke"):  # noqa: ANN001, ANN202
    from tests.manual.large_project_ab import Case

    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / "a.txt").write_text("hello", encoding="utf-8")
    return Case(name=name, project=project, task="Summarize a.txt. Do not modify files.", expected=())


def test_large_project_ab_arms_run_real_kernel(tmp_path) -> None:
    from tests.manual import large_project_ab as ab

    for arm in ("baseline", "current"):
        fake = FakeProvider(['{"tool": "done", "args": {"summary": "done summary"}}'])
        with mock.patch.object(ab, "connect_provider", return_value=fake):
            row = ab.run_arm(_make_case(tmp_path, f"large-{arm}"), arm, "fake", 0, 2)
        assert fake.prompts, arm
        assert row["stop_reason"] == "done", row
        assert row["arm"] == arm
        assert fake.closed is True
    # Experiment variable is the project map, not a codec.
    import inspect

    assert "codec" not in inspect.getsource(ab.run_arm)


def test_task_lens_ab_readonly_arm_runs_real_kernel(tmp_path) -> None:
    from tests.manual import task_lens_ab as ab

    project = tmp_path / "lens"
    project.mkdir(parents=True, exist_ok=True)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    case = ab.ProbeCase(
        name="smoke",
        task="Where is x defined?",
        tags=("smoke",),
        target_named_in_task=True,
        expected_paths=("a.py",),
        expected_tests=(),
    )
    for arm in ("current", "lens"):
        fake = FakeProvider(['{"tool": "done", "args": {"summary": "a.py defines x"}}'])
        with mock.patch.object(ab, "connect_provider", return_value=fake):
            row = ab.run_readonly_arm("fake", case, project, arm, port=0, max_turns=2)
        assert row["stop_reason"] == "done", row
        assert row["arm"] == arm


def test_context_delta_ab_arms_share_warmup_and_differ_followup(tmp_path) -> None:
    from tests.manual import context_delta_ab as ab

    project = tmp_path / "ctx"
    project.mkdir(parents=True, exist_ok=True)
    (project / "a.txt").write_text("hello", encoding="utf-8")
    case = ab.Case(
        name="smoke",
        project=project,
        warmup_task="Summarize a.txt. Do not modify files.",
        followup_task="Summarize a.txt again. Do not modify files.",
        expected=(),
    )
    for arm in ("full", "delta"):
        fake = FakeProvider(
            [
                '{"tool": "done", "args": {"summary": "warmup done"}}',
                '{"tool": "done", "args": {"summary": "followup done"}}',
            ]
        )
        with mock.patch.object(ab, "connect_provider", return_value=fake):
            row = ab.run_arm(case, arm, "fake", 0, 2)
        assert row["eligible"] is True, row
        assert row["warmup_stop_reason"] == "done", row
        assert row["followup_stop_reason"] == "done", row
