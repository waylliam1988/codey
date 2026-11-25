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
    unique: dict[str, None] = {}
    for match in _CJK.finditer(text):
        run = match.group()
        for i in range(len(run) - 1):
            unique[run[i : i + 2]] = None
    return list(unique)


def indexed_text(text: str) -> str:
    return text + "\n" + " ".join(cjk_terms(text))


def query_terms(text: str) -> list[str]:
    words: dict[str, None] = {}
    for match in re.finditer(r"[^\W_]+", text.casefold()):
        word = match.group()
        if word not in _STOP and not _CJK.search(word):
            words[word] = None
            if len(words) == 12:
                break
    return [*words, *cjk_terms(text)[:64]]
