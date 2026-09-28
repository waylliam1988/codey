"""Unicode digit + int hardening locks (red-first).

Root cause: ``str.isdigit()`` is True for unicode digits like "²" (U+00B2)
but ``int("²")`` raises ``ValueError``. Several helpers used
``isdigit()`` then bare ``int()``, so a single unicode key/field crashes
the caller. PubMed/source/tool-instance IDs used ``isdigit()`` as an
ASCII check, so "²" passed as valid (fail-open).

These tests assert the CLEANED state (fail-closed, no raw ValueError /
OverflowError, ASCII-only IDs), so they fail on pre-fix code and pass
after the hardening.
"""
from __future__ import annotations


def test_local_config_unicode_fail_closed() -> None:
    from codey.providers.local_config import _parse_positive_int

    # "²".isdigit() is True but int("²") raises; must not propagate.
    assert "²".isdigit()
    assert _parse_positive_int("²") is None
    assert _parse_positive_int("  ²  ") is None
    # valid inputs still work
    assert _parse_positive_int("12") == 12
    assert _parse_positive_int(5) == 5
    assert _parse_positive_int(True) is None


def test_protocol_unicode_fail_closed() -> None:
    from codey.agents.protocol import positive_int_value

    assert positive_int_value("²") is None
    assert positive_int_value("  ²  ".strip()) is None
    assert positive_int_value("12") == 12
    assert positive_int_value(True) is None


def test_adapter_unicode_keys_ignored() -> None:
    from codey.repairs.adapter_overrides import _next_generation, _trim_generations

    # unicode digit key must not crash; only ASCII "1" counts.
    assert _next_generation({"generations": {"²": {}, "1": {}}}) == 2
    assert _next_generation({"generations": {"²": {}}}) == 1
    # trim must not crash on unicode keys
    index = {
        "generations": {"²": {"a": 1}, "1": {"a": 1}, "2": {"a": 1}},
        "current_generation": 2,
        "previous_generation": 1,
        "provider_id": "",
    }
    _trim_generations(index)  # must not raise
    assert "²" in index["generations"] or "²" not in index["generations"]


def test_pdf_parse_unicode_fail_closed() -> None:
    from codey.research.pdf_extract import parse_pages

    # must not raise; falls back to default pages
    out = parse_pages("²")
    assert isinstance(out, tuple)
    assert all(isinstance(n, int) and n >= 1 for n in out)
    assert parse_pages("4") == (4,)


def test_bounded_positive_int_unicode_domain_error() -> None:
    import pytest

    from codey.toolchain.runtime import bounded_positive_int

    with pytest.raises(ValueError, match="must be a positive integer"):
        bounded_positive_int("²", "limit")
    assert bounded_positive_int("12", "limit") == 12


def test_workspace_config_unicode_fail_closed() -> None:
    from codey.workspace.config import _positive_int

    assert _positive_int("²") is None
    assert _positive_int("12") == 12
    assert _positive_int(True) is None


def test_shell_approval_unicode_fail_closed() -> None:
    from codey.agents.shell_approval import _nonnegative_int as shell_int

    assert shell_int("²") == 0
    assert shell_int("12") == 12
    assert shell_int(True) == 0


def test_tool_args_repair_unicode_domain_error() -> None:
    import pytest

    from codey.toolchain.tool_args_repair import (
        ToolArgsRepairError,
        _bounded_positive_int,
    )

    with pytest.raises(ToolArgsRepairError):
        _bounded_positive_int("²", "limit")
    # raw ValueError must never leak
    try:
        _bounded_positive_int("²", "limit")
    except ValueError as exc:
        assert isinstance(exc, ToolArgsRepairError), f"leaked raw ValueError: {exc!r}"
    val, _ = _bounded_positive_int("12", "limit")
    assert val == 12


def test_ui_state_int_bool_and_inf_fail_closed() -> None:
    from codey.storage.ui_state_store import _int

    # bool is never a count
    assert _int(True) == 0
    assert _int(False) == 0
    # inf/nan must not crash
    assert _int(float("inf")) == 0
    assert _int(float("-inf")) == 0
    assert _int(float("nan")) == 0
    assert _int("inf") == 0
    assert _int("12") == 12
    assert _int(None) == 0


def test_change_set_bool_fail_closed() -> None:
    from codey.workspace.change_set import _nonnegative_int as cs_int

    assert cs_int(True) == 0
    assert cs_int(False) == 0
    assert cs_int("12") == 12
    assert cs_int(None) == 0


def test_pubmed_unicode_fail_closed() -> None:
    from codey.research.source_connectors import is_valid_pubmed_id

    assert is_valid_pubmed_id("²") is False
    assert is_valid_pubmed_id("12") is True
    assert is_valid_pubmed_id("") is False


def test_pubmed_url_unicode_fail_closed() -> None:
    from codey.research.connector_search import _pubmed_id_from_url

    assert _pubmed_id_from_url("https://pubmed.ncbi.nlm.nih.gov/123/") == "123"
    assert _pubmed_id_from_url("https://pubmed.ncbi.nlm.nih.gov/²/") == ""


def test_source_id_unicode_fail_closed() -> None:
    from codey.research.controller import _looks_like_source_id

    assert _looks_like_source_id("s²") is False
    assert _looks_like_source_id("s12") is True
    assert _looks_like_source_id("x12") is False


def test_tool_instance_id_unicode_fail_closed() -> None:
    from codey.runs.trace_values import _tool_instance_id

    assert _tool_instance_id("²:³") == ""
    assert _tool_instance_id("1:2") == "1:2"
    assert _tool_instance_id("abc") == ""
