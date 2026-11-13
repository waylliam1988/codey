"""Explicit user web actions and their URLs, excluding code examples."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import groupby
from urllib.parse import urlsplit, urlunsplit

from codey.types.json_value import canonical_json_object

_CODE = re.compile(r"```[\s\S]*?```|`[^`\n]*`")
_URL = re.compile(r"https?://[^\s<>`\"'、，。；！？（）\[\]]+", re.IGNORECASE)
_CLAUSES = re.compile(r"[，。；;!?！？]|\.\s+|\.$")
_URL_TOKEN = re.compile(r"\x00\d+\x00")
_URL_LINE = re.compile(r"[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?"
                       r"(?:(?:Source|URL|来源)[ \t]*[:：][ \t]*)?"
                       r"(?:<\x00\d+\x00>|\[[^\]\n]+\]\(\x00\d+\x00\)|\x00\d+\x00)[ \t。；.!?]*", re.I)
_ACTION = re.compile(
    r"web_search|open_url|上网|联网查询|在线查找|查网页|查资料|官方文档|浏览网页|读取网页|"
    r"\b(?:browse|search the web|look online)\b", re.IGNORECASE)
_OPEN = re.compile(r"打开|访问|读取|浏览|看看|查看|分析|了解|\b(?:open|visit|inspect|read|look at)\b", re.IGNORECASE)
_NEGATED = re.compile(r"(?:不要|勿|禁止|无需|不需要|不用|不必|do not|don't|never|without)"
                      r"(?:\s|使用|用|再|去|帮我|请)*$", re.IGNORECASE)


@dataclass(frozen=True)
class WebIntent:
    requested: bool
    urls: frozenset[str]
    original_urls: frozenset[str] = frozenset()


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
    clauses: list[list[str]] = []
    for text_line in lines:
        boundaries = [0, *(match.end() for match in _CLAUSES.finditer(text_line))]
        ends = [*(match.start() for match in _CLAUSES.finditer(text_line)), len(text_line)]
        clauses.append([text_line[start:end].strip() for start, end in zip(boundaries, ends, strict=True)
                        if text_line[start:end].strip()])

    def url_line(index: int) -> bool:
        return _URL_LINE.fullmatch(lines[index]) is not None

    for links, group in groupby(range(len(lines)), key=url_line):
        if not links:
            continue
        rows = tuple(group)
        before, after = rows[0] - 1, rows[-1] + 1
        targets = [(before, -1), (after, 0)]
        for line, index in targets:
            if (0 <= line < len(lines) and clauses[line] and _open_clause(clauses[line][index])
                    and _URL_TOKEN.search(clauses[line][index]) is None):
                tokens = [match[0] for row in rows for match in _URL_TOKEN.finditer(lines[row])]
                clauses[line][index] += " " + " ".join(tokens)
                break
        for row in rows:
            clauses[row].clear()
    return [clause for line in clauses for clause in line]


def explicit_web_intent(task: str) -> WebIntent:
    text = _CODE.sub("", task)
    url_tokens: dict[str, tuple[str, str | None]] = {}

    def protect_url(match: re.Match[str]) -> str:
        token = f"\x00{len(url_tokens)}\x00"
        value = match[0]
        address = value.rstrip(").,:;!?")
        # A closing Markdown parenthesis has an explicit boundary. Other
        # stripped ASCII punctuation may belong to the URL; do not bind it.
        markdown = value == address + ")" and re.search(r"\[[^\]\n]+\]\($", text[:match.start()]) is not None
        original = address if value == address or markdown else None
        url_tokens[token] = (source_url_key(address), original)
        return token + value[len(address):]

    text = _URL.sub(protect_url, text)
    requested = False
    direct: dict[str, set[str | None] | None] = {}
    for clause in _request_clauses(text):
        targets = list(_URL_TOKEN.finditer(clause))
        actions = list(_ACTION.finditer(clause))
        if targets:
            actions.extend(_OPEN.finditer(clause))
        actions.sort(key=lambda match: match.start())
        permissions = [(action, _NEGATED.search(clause[:action.start()]) is None) for action in actions]
        requested = requested or any(positive for _, positive in permissions)
        for target in targets:
            if target[0] not in url_tokens or not permissions:
                continue
            preceding = [(action, positive) for action, positive in permissions if action.start() < target.start()]
            action, positive = preceding[-1] if preceding else permissions[0]
            key, original = url_tokens[target[0]]
            if not positive:
                direct[key] = None
                continue
            candidates = direct.get(key)
            if key in direct and candidates is None and (
                    not _open_clause(action[0]) or action[0] in {"分析", "了解"}):
                continue
            if candidates is None:
                direct[key] = {original}
            else:
                candidates.add(original)
    allowed = {key: candidates for key, candidates in direct.items() if candidates is not None}
    originals = {url for candidates in allowed.values() for url in candidates if url is not None}
    unambiguous = all(None not in candidates for candidates in allowed.values())
    return WebIntent(requested, frozenset(allowed), frozenset(originals) if unambiguous else frozenset())


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
    payload = canonical_json_object(state["source_requirements"])
    if (set(payload) != {"version", "required_urls"}
            or type(payload["version"]) is not int or payload["version"] != 1):
        raise ValueError("invalid source requirement snapshot")
    urls = payload["required_urls"]
    if not isinstance(urls, (list, tuple)):
        raise ValueError("invalid required source URLs")
    restored: set[str] = set()
    for url in urls:
        if not isinstance(url, str) or not url or source_url_key(url) != url:
            raise ValueError("invalid required source URLs")
        restored.add(url)
    return frozenset(restored)
