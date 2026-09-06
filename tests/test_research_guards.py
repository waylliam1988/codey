from __future__ import annotations

from codey.research.guards import (
    bounded_int,
    clip_schema_ok,
    identifier_schema_ok,
    status_token,
)
from codey.research.shape import bounded_limit


def test_bounded_int_uses_default_and_bounds() -> None:
    assert bounded_int("bad", 2, 8, default=5) == 5
    assert bounded_int("99", 2, 8) == 8
    assert bounded_int("-1", 2, 8) == 2


def test_shape_bounded_limit_preserves_bool_as_default() -> None:
    assert bounded_limit(True, default=4, upper=8) == 4
    assert bounded_limit(False, default=4, upper=8) == 4


def test_status_token_normalizes_allowed_research_statuses() -> None:
    allowed = {"answered", "not_answered"}

    assert status_token("Answered", allowed, default="not_answered") == "answered"
    assert status_token("unsupported", allowed, default="not_answered") == "not_answered"


def test_schema_guards_require_canonical_strings() -> None:
    assert identifier_schema_ok("answer_status", 40)
    assert not identifier_schema_ok("answer status", 40)
    assert clip_schema_ok("short", 10)
    assert not clip_schema_ok("too long", 6)
