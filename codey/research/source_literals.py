"""Literal version anchors, never a proof of which behavior a version governs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_VERSION = re.compile(r"(?<![\d.])[vV]?\d+(?:\.\d+){2,3}"
                      r"(?:[-+][a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?)?(?!\d|\.\d)")


def version_mentions(text: str) -> list[dict[str, Any]]:
    return [{"literal": match[0], "start": match.start(), "end": match.end()}
            for match in _VERSION.finditer(text)]


def _key(literal: str) -> str:
    return literal.removeprefix("v").removeprefix("V")


def validate_version_literals(claim: str, excerpts: Sequence[str]) -> None:
    """Reject introduced version literals; matching literals remain unreviewed.

    In particular, both 3.7.0 and 3.22.0 occurring together cannot establish
    which one qualifies read-only operation, nor the meaning of before/since.
    """
    available = {_key(row["literal"]) for text in excerpts for row in version_mentions(text)}
    missing = list(dict.fromkeys(row["literal"] for row in version_mentions(claim)
                                if _key(row["literal"]) not in available))
    if missing:
        raise ValueError("version literal absent from selected original excerpts: " + ", ".join(missing)
            + ". Read and select the exact supporting passage or correct the local claim; "
            "do not substitute a nearby version. Matching a number does not establish its behavior or condition.")


def version_anchors(claim: str, passages: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Bounded read-only preview; full conditions stay in the original passages."""
    requested = list(dict.fromkeys(_key(row["literal"]) for row in version_mentions(claim)))
    rows: list[dict[str, Any]] = []
    total = 0
    for ref, text in passages:
        for mention in version_mentions(text):
            key = _key(mention["literal"])
            if key not in requested:
                continue
            total += 1
            if len(rows) >= 8:
                continue
            start, end = max(0, mention["start"] - 100), min(len(text), mention["end"] + 100)
            rows.append({"passage_ref": ref, **mention, "context": text[start:end],
                         "context_start": start, "context_end": end, "context_complete": start == 0 and end == len(text)})
    return {"claim_mentions": version_mentions(claim), "original_mentions": rows,
            "display_complete": total == len(rows), "interpretation_review": "unreviewed"}
