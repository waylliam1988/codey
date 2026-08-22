from __future__ import annotations

from tests.manual.ab_journal import sanitize_facts


def test_typed_scalar_facts_survive() -> None:
    cleaned = sanitize_facts({
        "input_empty": True,
        "question_count_increased": True,
        "response_chars": 4211,
        "profile_hash": "sha256:abcdef",
        "elapsed_ms": 1234.5678,
        "failure_kind": "timeout",
        "failure_stage": "wait_reply",
    })

    assert cleaned == {
        "elapsed_ms": 1234.5678,
        "failure_kind": "timeout",
        "failure_stage": "wait_reply",
        "input_empty": True,
        "profile_hash": "sha256:abcdef",
        "question_count_increased": True,
        "response_chars": 4211,
    }


def test_url_html_and_cookie_values_are_redacted() -> None:
    cleaned = sanitize_facts({
        "last_seen_url": "https://chat.example.com/session/abc",
        "page_snippet": "<div class='reply'>hello</div>",
        "cookie_note": "sessionid=xyz",
    })

    assert cleaned["last_seen_url"] == "[redacted]"
    assert cleaned["page_snippet"] == "[redacted]"
    # Cookie-ish keys are dropped outright instead of redacted.
    assert "cookie_note" not in cleaned


def test_secret_shaped_values_are_redacted() -> None:
    cleaned = sanitize_facts({
        "auth_header": "authorization bearer abcdef",
        "client_note": "api key is sk-abcdef1234567890abcdef",
        "safe_marker_word": "token_budget_exceeded",
    })

    assert cleaned["auth_header"] == "[redacted]"
    assert cleaned["client_note"] == "[redacted]"
    # Ordinary reason-code wording that merely contains a marker word survives.
    assert cleaned["safe_marker_word"] == "token_budget_exceeded"


def test_sensitive_keys_are_dropped_entirely() -> None:
    cleaned = sanitize_facts({
        "dom_snapshot": {"tag": "button"},
        "html_body": "<html><body>x</body></html>",
        "raw_stdout": "traceback lines",
        "webpage_text": "provider page words",
        "ok_fact": True,
    })

    assert cleaned == {"ok_fact": True}


def test_strings_are_clipped_and_lists_bounded() -> None:
    cleaned = sanitize_facts({
        "long_note": "x" * 500,
        "stages": [f"stage-{i}" for i in range(20)],
    })

    assert len(cleaned["long_note"]) == 200
    assert len(cleaned["stages"]) == 8


def test_nested_provider_failure_is_allow_listed() -> None:
    cleaned = sanitize_facts({
        "provider_failure": {
            "kind": "timeout",
            "stage": "wait_reply",
            "message": "raw provider error body should not persist",
            "title": "page title leak",
            "observed_at": "2026-08-22T00:00:00Z",
            "action": "click submit",
        },
    })

    # Only the typed observation vocabulary survives the boundary.
    assert cleaned == {
        "provider_failure_kind": "timeout",
        "provider_failure_stage": "wait_reply",
    }


def test_unknown_nested_mappings_are_dropped_entirely() -> None:
    cleaned = sanitize_facts({
        "weird": {"nested": "dict"},
        "page_state": {"typing": True},
        "also_weird": object(),
        "fine": False,
    })

    # Generic nested maps have no allow-list entry: dropped, not flattened.
    assert cleaned == {"fine": False}


def test_nonfinite_floats_are_dropped() -> None:
    import math

    cleaned = sanitize_facts({
        "elapsed_ms": float("inf"),
        "ratio": float("nan"),
        "ok_ms": 12.5,
    })

    assert math.isfinite(cleaned["ok_ms"])
    assert "elapsed_ms" not in cleaned
    assert "ratio" not in cleaned


def test_opaque_objects_are_dropped() -> None:
    cleaned = sanitize_facts({
        "also_weird": object(),
        "fine": False,
    })

    assert cleaned == {"fine": False}

    facts = {f"fact_{index:02}": "y" * 200 for index in range(100)}
    encoded = __import__("json").dumps(sanitize_facts(facts), ensure_ascii=False)
    assert len(encoded.encode("utf-8")) <= 8 * 1024


def test_non_mapping_input_yields_empty_facts() -> None:
    assert sanitize_facts(None) == {}
    assert sanitize_facts("not-a-mapping") == {}  # type: ignore[arg-type]
