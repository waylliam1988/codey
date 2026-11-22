"""Provider timeout metadata retains omission, None, zero, and explicit values."""

import pytest

from codey.agents.consensus import run_consensus
from tests.test_consensus import FakeProvider


@pytest.mark.parametrize("timeout", [None, 0.0, 180.0])
def test_explicit_timeout_metadata_reaches_each_send_unchanged(timeout: float | None) -> None:
    selected = FakeProvider(["draft", "final"])
    selected.timeout = timeout
    advisor = FakeProvider(["advice"])
    advisor.timeout = timeout

    result = run_consensus(
        selected_provider=selected, selected_provider_id="owner", task="answer",
        provider_ids=("owner", "advisor"), provider_labels={}, availability=lambda: {"advisor": True},
        connect_existing=lambda _: advisor, draft_first=True,
    )

    assert result is not None and result.answer == "final"
    assert selected.timeouts == [timeout, timeout]
    assert advisor.timeouts == [timeout]


@pytest.mark.parametrize("timeout", ["not a number", {"seconds": 1}])
def test_invalid_timeout_metadata_is_rejected_before_owner_send(
    monkeypatch: pytest.MonkeyPatch, timeout: object,
) -> None:
    selected = FakeProvider(["draft", "final"])
    monkeypatch.setattr(selected, "timeout", timeout, raising=False)
    advisor = FakeProvider(["advice"])

    with pytest.raises(TypeError, match="timeout"):
        run_consensus(
            selected_provider=selected, selected_provider_id="owner", task="answer",
            provider_ids=("owner", "advisor"), provider_labels={}, availability=lambda: {"advisor": True},
            connect_existing=lambda _: advisor, draft_first=True,
        )

    assert not selected.sent and not advisor.sent


def test_dynamic_warning_hooks_keep_their_existing_callback_behavior() -> None:
    warnings: list[str] = []

    class TraceProxy:
        def __getattr__(self, name: str) -> object:
            if name != "warn":
                raise AttributeError(name)

            def warn(reason_code: str) -> None:
                warnings.append(reason_code)

            return warn

    selected = FakeProvider(["draft"])
    advisor = FakeProvider(fail=True)

    result = run_consensus(
        selected_provider=selected, selected_provider_id="owner", task="answer",
        provider_ids=("owner", "advisor"), provider_labels={}, availability=lambda: {"advisor": True},
        connect_existing=lambda _: advisor, draft_first=True, trace_recorder=TraceProxy(),
    )

    assert result is not None and result.degraded
    assert warnings == ["advisor_failed:advisor"]
