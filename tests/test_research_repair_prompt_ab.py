from tests.manual import research_repair_prompt_ab as probe


def test_repair_prompt_ab_compares_old_and_new_boundaries() -> None:
    old = probe._old_prompt()
    new = probe._new_prompt()

    assert "Allowed JSON shapes:" in old
    assert "Allowed JSON shapes for this turn (choose exactly one):" in new
    assert "Tools not listed here are forbidden this turn" in new
    assert "Do not repeat the same JSON object twice." in new


def test_repair_prompt_ab_scores_allowed_and_disallowed_replies() -> None:
    allowed = probe._analyze_reply('{"tool":"web_search","args":{"query":"铝合金 美伊战争"}}')
    disallowed = probe._analyze_reply(
        '{"tool":"knowledge_write","args":{"title":"铝合金与美伊战争","content":"..."} }'
    )

    assert allowed["accepted"] is True
    assert allowed["accepted_tool"] == "web_search"
    assert disallowed["accepted"] is False
    assert disallowed["protocol_error_kind"] == "disallowed_tool"
