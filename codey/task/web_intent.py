"""Explicit user web actions and their URLs, excluding code examples."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import groupby
from urllib.parse import urlsplit, urlunsplit

_CODE = re.compile(r"```[\s\S]*?```|`[^`\n]*`")
_URL = re.compile(r"https?://[^\s<>`\"'、，。；！？（）\[\]]+", re.IGNORECASE)
_CLAUSES = re.compile(r"[，。；;!?！？]|\.\s+|\.$")
_URL_TOKEN = re.compile(r"\x00\d+\x00")
_URL_LINE = re.compile(r"[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?"
                       r"(?:<\x00\d+\x00>|\[[^\]\n]+\]\(\x00\d+\x00\)|\x00\d+\x00)[ \t。；.!?]*")
_ACTION = re.compile(
    r"web_search|open_url|上网|联网查询|在线查找|查网页|查资料|官方文档|浏览网页|读取网页|"
    r"\b(?:browse|search the web|look online)\b", re.IGNORECASE)
_OPEN = re.compile(r"打开|访问|看看|查看|分析|了解|\b(?:open|visit|inspect|read|look at)\b", re.IGNORECASE)
_NEGATED = re.compile(r"(?:不要|勿|禁止|无需|不需要|不用|不必|do not|don't|never|without)"
                      r"(?:\s|使用|用|再|去|帮我|请)*$", re.IGNORECASE)


@dataclass(frozen=True)
class WebIntent:
    requested: bool
    urls: frozenset[str]


def source_url_key(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        # User/tool input is validated by the acquisition network policy.
        return url
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _open_clause(clause: str) -> bool:
    return bool(_OPEN.search(clause) or re.search(r"\bopen_url\b", clause, re.I))


def _request_clauses(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    clauses = [[part.strip() for part in _CLAUSES.split(line) if part.strip()] for line in lines]
    for links, group in groupby(range(len(lines)), key=lambda i: _URL_LINE.fullmatch(lines[i]) is not None):
        if not links:
            continue
        rows = tuple(group)
        before, after = rows[0] - 1, rows[-1] + 1
        targets = [(before, -1), (after, 0)]
        for line, index in targets:
            if 0 <= line < len(lines) and clauses[line] and _open_clause(clauses[line][index]):
                tokens = [token for row in rows for token in _URL_TOKEN.findall(lines[row])]
                clauses[line][index] += " " + " ".join(tokens)
                break
        for row in rows:
            clauses[row].clear()
    return [clause for line in clauses for clause in line]


def explicit_web_intent(task: str) -> WebIntent:
    text = _CODE.sub("", task)
    url_tokens: dict[str, str] = {}

    def protect_url(match: re.Match[str]) -> str:
        token = f"\x00{len(url_tokens)}\x00"
        value = match[0]
        address = value.rstrip(").,:;!?")
        url_tokens[token] = source_url_key(address)
        return token + value[len(address):]

    text = _URL.sub(protect_url, text)
    requested = False
    direct: set[str] = set()
    for clause in _request_clauses(text):
        clause_urls = {url_tokens[token] for token in _URL_TOKEN.findall(clause) if token in url_tokens}
        actions = list(_ACTION.finditer(clause))
        if clause_urls:
            actions.extend(_OPEN.finditer(clause))
        if any(_NEGATED.search(clause[:match.start()]) is None for match in actions):
            requested = True
            direct.update(clause_urls)
    return WebIntent(requested, frozenset(direct))


def source_requirements(task: str) -> dict[str, object]:
    """Freeze explicit source requirements at task admission."""
    return {"version": 1, "required_urls": sorted(explicit_web_intent(task).urls)}


def restored_source_requirements(state: Mapping[str, object]) -> frozenset[str]:
    """Decode the entry snapshot; legacy tasks use their stored original input."""
    if "source_requirements" not in state:
        task = state.get("input_text", state.get("task", ""))
        if not isinstance(task, str):
            raise ValueError("original source requirement input must be text")
        return explicit_web_intent(task).urls
    payload = state["source_requirements"]
    if (not isinstance(payload, Mapping) or set(payload) != {"version", "required_urls"}
            or type(payload["version"]) is not int or payload["version"] != 1):
        raise ValueError("invalid source requirement snapshot")
    urls = payload["required_urls"]
    if (not isinstance(urls, (list, tuple)) or any(not isinstance(url, str) or not url
            or source_url_key(url) != url for url in urls)):
        raise ValueError("invalid required source URLs")
    return frozenset(urls)
