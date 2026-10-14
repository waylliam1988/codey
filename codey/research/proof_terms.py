"""Question-term extraction and safe coverage matching for proof review."""

from __future__ import annotations

import re

from codey.policies.redaction import looks_prompt_visible_secret
from codey.utils.refs import identifier

_STOP_TERMS = frozenset(
    {
        "about",
        "answer",
        "are",
        "can",
        "does",
        "find",
        "for",
        "how",
        "investigate",
        "need",
        "please",
        "question",
        "research",
        "should",
        "study",
        "the",
        "this",
        "what",
        "whether",
        "when",
        "where",
        "which",
        "why",
        "with",
        "研究",
        "调查",
        "查找",
        "问题",
        "什么",
        "如何",
        "是否",
    }
)


def question_terms(question: str) -> tuple[str, ...]:
    text = str(question or "")
    tokens: list[str] = []
    quoted = re.findall(r"['\"]([^'\"]{2,80})['\"]", text)
    for item in quoted:
        _append_term(tokens, item)
    for item in re.findall(
        r"[A-Za-z][A-Za-z0-9_-]{2,}|20\d{2}|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}",
        text,
    ):
        _append_term(tokens, item)
        if len(tokens) >= 12:
            break
    return tuple(tokens)


def safe_term_ref(term: str) -> str:
    return identifier(term, 80) if is_safe_signal_text(term) else ""


def is_safe_signal_text(value: str) -> bool:
    text = str(value or "").strip()
    lower = text.casefold()
    if not text or len(text) > 100:
        return False
    if "://" in lower or "\\" in text or "/" in text:
        return False
    if looks_prompt_visible_secret(text):
        return False
    return not re.fullmatch(r"[A-Za-z0-9_./+=-]{24,}", text)


def normalized_search_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def search_term_present(term: str, haystack: str) -> bool:
    """Match a question term as a token, never as an arbitrary substring."""
    normalized = normalized_search_text(term)
    if not normalized:
        return False
    if re.search(r"[a-z0-9_]", normalized):
        escaped = re.escape(normalized).replace(r"\ ", r"\s+")
        return re.search(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])", haystack) is not None
    return normalized in haystack


def _append_term(tokens: list[str], value: str) -> None:
    term = _clean_term(value)
    if not term or term in tokens or term in _STOP_TERMS:
        return
    if is_safe_signal_text(term):
        tokens.append(term)


def _clean_term(value: str) -> str:
    text = str(value or "").strip().strip(".,;:!?()[]{}<>，。；：！？（）【】")
    if len(text) > 80:
        return ""
    return text.casefold()

