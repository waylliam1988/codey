"""Derived lexical terms for multilingual FTS; never entity aliases or facts."""

import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "is",
    "in",
    "how",
    "what",
    "with",
    "can",
    "for",
    "not",
    "on",
    "at",
    "from",
    "by",
    "as",
    "be",
    "we",
}
SEARCH_INDEX_VERSION = "cjk-bigram-1"


def cjk_terms(text: str) -> list[str]:
    return list(dict.fromkeys(run[i : i + 2] for run in _CJK.findall(text) for i in range(len(run) - 1)))


def indexed_text(text: str) -> str:
    return text + "\n" + " ".join(cjk_terms(text))


def query_terms(text: str) -> list[str]:
    words = [term for term in re.findall(r"[^\W_]+", text.casefold()) if term not in _STOP and not _CJK.search(term)]
    return list(dict.fromkeys([*words[:12], *cjk_terms(text)[:64]]))
