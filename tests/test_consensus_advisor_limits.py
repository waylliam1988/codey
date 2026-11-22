"""An explicit advisor cap applies before any provider interaction."""

import pytest

from codey.agents.consensus import advisor_ids, run_consensus
from codey.providers.base import ChatProvider


@pytest.mark.parametrize("limit", [0, 1, 2, 10])
def test_advisor_selection_respects_the_requested_maximum(limit: int) -> None:
    selected = advisor_ids("owner", {"a": True, "b": True, "c": True},
                           ("owner", "a", "b", "c"), max_advisors=limit)

    assert selected == ("a", "b", "c")[:limit]


@pytest.mark.parametrize("draft_first", [False, True])
def test_zero_advisor_cap_does_not_open_or_send_provider_messages(draft_first: bool) -> None:
    interactions: list[str] = []

    class Provider:
        name = "fixture"
        location = "fixture"

        def new_chat(self, timeout: float | None = None) -> None:
            interactions.append("new_chat")

        def send(self, text: str, timeout: float | None = None) -> str:
            interactions.append("send")
            return "fixture answer"

        def close(self) -> None:
            interactions.append("close")

    def connect(provider_id: str) -> ChatProvider:
        interactions.append("connect")
        return Provider()

    result = run_consensus(
        selected_provider=Provider(), selected_provider_id="owner", task="answer",
        provider_ids=("owner", "a"), provider_labels={}, availability=lambda: {"a": True},
        connect_existing=connect, max_advisors=0, draft_first=draft_first,
    )

    assert result is None
    assert interactions == []
