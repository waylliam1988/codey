"""Context follow-up compares continued windows against fresh handoff windows.

Locks: warmup is identical; continued reuses the same conversation window
without a new chat; fresh opens a new chat and carries the factual
handoff in the real kernel prompt; both arms run the real kernel on a
temporary project, change nothing, and close the provider.
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


def _case(tmp_path: Path, name: str = "smoke"):
    from tests.manual.context_delta_ab import Case

    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / "a.txt").write_text("hello", encoding="utf-8")
    return Case(
        name=name, project=project,
        warmup_task="Summarize a.txt. Do not modify files.",
        followup_task="Summarize a.txt again. Do not modify files.",
        expected=(),
    )


def test_session_window_vs_handoff_differs_in_prompt_and_chat_lifecycle(tmp_path):
    from tests.manual import context_delta_ab as ab

    assert set(ab.ARMS) == {"continued", "fresh-handoff"}
    rows = {}
    providers = {}
    for arm in ab.ARMS:
        fake = FakeProvider(
            [
                '{"tool": "done", "args": {"summary": "warmup done"}}',
                '{"tool": "done", "args": {"summary": "followup done"}}',
            ]
        )
        providers[arm] = fake
        with mock.patch.object(ab, "connect_provider", return_value=fake):
            rows[arm] = ab.run_arm(_case(tmp_path, f"ctx-{arm}"), arm, "fake", 0, 2)
    for arm, row in rows.items():
        assert row["eligible"] is True, row
        assert row["warmup_stop_reason"] == "done", row
        assert row["followup_stop_reason"] == "done", row
        assert providers[arm].closed is True
    assert providers["continued"].new_chat_calls == 1, "warmup only"
    assert providers["fresh-handoff"].new_chat_calls == 2, "warmup + fresh followup"
    continued_prompts = providers["continued"].prompts
    handoff_prompts = providers["fresh-handoff"].prompts
    assert continued_prompts[0] == handoff_prompts[0], "warmup must be identical"
    assert handoff_prompts[-1] != continued_prompts[-1], "followup must differ"
    assert "Factual handoff" in handoff_prompts[-1]
    assert "Factual handoff" not in continued_prompts[-1]
