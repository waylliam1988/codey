"""Explicit user web actions and their URLs, excluding code examples."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_CODE = re.compile(r"```[\s\S]*?```|`[^`\n]*`")
_URL = re.compile(r"https?://[^\s<>`\"'、，。；！？（）\[\]]+", re.IGNORECASE)
_CLAUSES = re.compile(r"[，。；;!?！？\n]|\.\s+")
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


def explicit_web_intent(task: str) -> WebIntent:
    text = _CODE.sub("", task)
    url_tokens: dict[str, str] = {}

    def protect_url(match: re.Match[str]) -> str:
        token = f"\x00{len(url_tokens)}\x00"
        url_tokens[token] = source_url_key(match.group().rstrip(").,:;!?"))
        return token

    text = _URL.sub(protect_url, text)
    urls = frozenset(url_tokens.values())
    requested = False
    direct: set[str] = set()
    for clause in _CLAUSES.split(text):
        clause_urls = frozenset(url for token, url in url_tokens.items() if token in clause)
        masked = clause
        actions = list(_ACTION.finditer(masked))
        if clause_urls or (urls and any(word in clause for word in ("网站", "网址", "链接", "website", "URL"))):
            actions.extend(_OPEN.finditer(masked))
        if any(_NEGATED.search(masked[:match.start()]) is None for match in actions):
            requested = True
            direct.update(clause_urls)
            if not clause_urls and any(_NEGATED.search(masked[:match.start()]) is None
                                      for match in _OPEN.finditer(masked)):
                direct.update(urls)
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
