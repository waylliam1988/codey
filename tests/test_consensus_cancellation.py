"""Optional advisor failures must not consume an explicit task stop."""

import pytest

from codey.agents.consensus import run_consensus
from codey.providers.base import ChatProvider
from codey.runtime.core.cancellation import TaskCancelled
from tests.test_consensus import FakeProvider, TraceRecorder


@pytest.mark.parametrize("stage", [
    "availability", "advisor_connect", "advisor_send", "warning", "advisor_close", "owner_send",
])
def test_task_cancellation_propagates_from_each_consultation_boundary(stage: str) -> None:
    class Advisor(FakeProvider):
        def close(self) -> None:
            super().close()
            if stage == "advisor_close":
                raise TaskCancelled("stop")

    class StopTrace(TraceRecorder):
        def warn(self, reason_code: str) -> None:
            raise TaskCancelled("stop")

    selected = FakeProvider(["draft", TaskCancelled("stop")] if stage == "owner_send" else ["draft", "final"])
    advisor = Advisor([TaskCancelled("stop")] if stage == "advisor_send" else ["advice"], fail=stage == "warning")

    def availability() -> dict[str, bool]:
        if stage == "availability":
            raise TaskCancelled("stop")
        return {"advisor": True}

    def connect(provider_id: str) -> ChatProvider:
        if stage == "advisor_connect":
            raise TaskCancelled("stop")
        return advisor

    with pytest.raises(TaskCancelled, match="stop"):
        run_consensus(
            selected_provider=selected, selected_provider_id="owner", task="answer",
            provider_ids=("owner", "advisor"), provider_labels={}, availability=availability,
            connect_existing=connect, draft_first=True, trace_recorder=StopTrace() if stage == "warning" else None,
        )

    if stage in {"advisor_send", "warning", "advisor_close", "owner_send"}:
        assert advisor.closed
    if stage == "availability":
        assert not selected.sent


def test_an_ordinary_cleanup_failure_keeps_the_completed_advice() -> None:
    class Advisor(FakeProvider):
        def close(self) -> None:
            super().close()
            raise RuntimeError("connection cleanup failed")

    selected = FakeProvider(["final"])
    advisor = Advisor(["advice"])

    result = run_consensus(
        selected_provider=selected, selected_provider_id="owner", task="answer",
        provider_ids=("owner", "advisor"), provider_labels={}, availability=lambda: {"advisor": True},
        connect_existing=lambda _: advisor,
    )

    assert result is not None and result.answer == "final"
    assert result.advisor_count == 1 and advisor.closed
