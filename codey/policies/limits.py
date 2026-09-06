"""Shared local execution limits."""

from __future__ import annotations


SHELL_TIMEOUT = 120
SHELL_OUTPUT_LIMIT = 24_000
REVIEW_TIMEOUT = 300.0


__all__ = [
    "REVIEW_TIMEOUT",
    "SHELL_OUTPUT_LIMIT",
    "SHELL_TIMEOUT",
]
