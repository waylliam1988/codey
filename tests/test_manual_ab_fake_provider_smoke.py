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
        self.new_chat_calls = 0
        self.closed = False

    def new_chat(self) -> None:
        self.new_chat_calls += 1
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

    assert tuple(ab.ARMS) == ("continued", "fresh-handoff")
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
    rows: dict[str, dict] = {}
    fakes: dict[str, FakeProvider] = {}
    for arm in ab.ARMS:
        fake = FakeProvider(
            [
                '{"tool": "done", "args": {"summary": "warmup done"}}',
                '{"tool": "done", "args": {"summary": "followup done"}}',
            ]
        )
        fakes[arm] = fake
        with mock.patch.object(ab, "connect_provider", return_value=fake):
            rows[arm] = ab.run_arm(case, arm, "fake", 0, 2)
    for arm, row in rows.items():
        assert row["eligible"] is True, row
        assert row["warmup_stop_reason"] == "done", row
        assert row["followup_stop_reason"] == "done", row
        assert row["experiment"] == "session_window_with_factual_handoff", row
        assert fakes[arm].closed is True, arm
    assert fakes["continued"].prompts[0] == fakes["fresh-handoff"].prompts[0]
    assert fakes["continued"].new_chat_calls == 1, "continued reuses the warmup window"
    assert fakes["fresh-handoff"].new_chat_calls == 2, "fresh-handoff opens a new chat"
    assert "Factual handoff" not in fakes["continued"].prompts[-1]
    assert "Factual handoff" in fakes["fresh-handoff"].prompts[-1]
    assert (project / "a.txt").read_text(encoding="utf-8") == "hello"
