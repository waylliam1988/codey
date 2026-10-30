"""Recognize explicit investigation gaps, without judging contrary evidence."""

from __future__ import annotations

import re

_INCOMPLETE = re.compile(
    r"(?:尚未|未曾|没有|未)(?:系统|独立|充分|专门)?(?:调查|检索|搜索|核查|检查)(?:过)?(?:强)?反证"
    r"|反证(?:检索|搜索|调查|核查)?(?:尚未|仍未|未)(?:完成|调查|检索|搜索|核查)"
    r"|(?:counterevidence|contrary evidence)(?: search| investigation)? "
    r"(?:was |has |is )?(?:not (?:been )?(?:investigated|searched|checked|completed)|remains incomplete)"
    r"|(?:did not|have not|haven't) (?:investigate|search for|check) (?:counterevidence|contrary evidence)",
    re.IGNORECASE,
)


def counter_investigation_incomplete(text: str) -> bool:
    """A declaration of an unfinished check must never count as a check."""
    return bool(_INCOMPLETE.search(" ".join(text.split())))
