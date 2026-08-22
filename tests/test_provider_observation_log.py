from __future__ import annotations

from tests.manual.ab_journal import sanitize_facts


def test_typed_scalar_facts_survive() -> None:
    cleaned = sanitize_facts(
        "timeout",
        {
            "input_empty": True,
            "question_count_increased": True,
            "response_chars": 4211,
            "profile_hash": "sha256:abcdef",
            "elapsed_ms": 1234.5678,
            "failure_kind": "timeout",
            "failure_stage": "wait_reply",
        },
    )

    assert cleaned == {
        "elapsed_ms": 1234.5678,
        "failure_kind": "timeout",
        "failure_stage": "wait_reply",
        "input_empty": True,
        "profile_hash": "sha256:abcdef",
        "question_count_increased": True,
        "response_chars": 4211,
    }


def test_unknown_fact_names_are_dropped_even_when_values_are_safe() -> None:
    cleaned = sanitize_facts(
        "timeout",
        {
            "failure_kind": "timeout",
            "cookies": "sessionid=xyz",
            "page_text": "provider page words",
            "response_text": "raw answer body",
            "ok_fact": True,
        },
    )

    assert cleaned == {"failure_kind": "timeout"}


def test_url_html_and_cookie_values_are_redacted_on_allowed_keys() -> None:
    cleaned = sanitize_facts(
        "timeout",
        {
            "profile_hash": "https://chat.example.com/session/abc",
            "failure_stage": "<div class='reply'>hello</div>",
            "failure_kind": "session cookie appeared",
        },
    )

    assert cleaned["profile_hash"] == "[redacted]"
    assert cleaned["failure_stage"] == "[redacted]"
    assert cleaned["failure_kind"] == "[redacted]"


def test_secret_shaped_values_are_redacted() -> None:
    cleaned = sanitize_facts(
        "timeout",
        {
            "failure_stage": "authorization bearer abcdef",
            "profile_hash": "api key is sk-abcdef1234567890abcdef",
            "failure_kind": "token_budget_exceeded",
        },
    )

    assert cleaned["failure_stage"] == "[redacted]"
    assert cleaned["profile_hash"] == "[redacted]"
    # Ordinary reason-code wording that merely contains a marker word survives.
    assert cleaned["failure_kind"] == "token_budget_exceeded"


def test_unknown_event_type_drops_everything() -> None:
    cleaned = sanitize_facts("unknown_event", {"failure_kind": "timeout"})

    assert cleaned == {}


def test_strings_are_clipped_and_lists_bounded() -> None:
    cleaned = sanitize_facts(
        "note",
        {
            "output": "x" * 500,
            "cases": [f"stage-{i}" for i in range(20)],
        },
    )

    assert len(cleaned["output"]) == 200
    assert len(cleaned["cases"]) == 8


def test_nested_provider_failure_is_allow_listed() -> None:
    cleaned = sanitize_facts(
        "send_error",
        {
            "provider_failure": {
                "kind": "timeout",
                "stage": "wait_reply",
                "message": "raw provider error body should not persist",
                "title": "page title leak",
                "observed_at": "2026-08-22T00:00:00Z",
                "action": "click submit",
            },
        },
    )

    # Only the typed observation vocabulary survives the boundary.
    assert cleaned == {
        "provider_failure_kind": "timeout",
        "provider_failure_stage": "wait_reply",
    }


def test_unknown_nested_mappings_are_dropped_entirely() -> None:
    cleaned = sanitize_facts(
        "send_error",
        {
            "error_class": "TimeoutError",
            "weird": {"nested": "dict"},
            "page_state": {"typing": True},
            "also_weird": object(),
        },
    )

    # Generic nested maps have no allow-list entry: dropped, not flattened.
    assert cleaned == {"error_class": "TimeoutError"}


def test_nonfinite_floats_are_dropped() -> None:
    cleaned = sanitize_facts(
        "timeout",
        {
            "elapsed_ms": float("inf"),
            "response_chars": float("nan"),
            "failure_stage": "wait_reply",
        },
    )

    assert cleaned == {"failure_stage": "wait_reply"}
    assert "elapsed_ms" not in cleaned
    assert "response_chars" not in cleaned


def test_opaque_objects_are_dropped() -> None:
    cleaned = sanitize_facts("case_complete", {"ok": False, "stop_reason": object()})

    assert cleaned == {"ok": False}

    facts = {"output": "y" * 10000, "cases": ["z" * 500 for _ in range(100)]}
    encoded = __import__("json").dumps(sanitize_facts("note", facts), ensure_ascii=False)
    assert len(encoded.encode("utf-8")) <= 8 * 1024


def test_non_mapping_input_yields_empty_facts() -> None:
    assert sanitize_facts("note", None) == {}
    assert sanitize_facts("note", "not-a-mapping") == {}  # type: ignore[arg-type]
