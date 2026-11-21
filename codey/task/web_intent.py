"""Explicit user web actions and their URLs, excluding code examples."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import groupby
from urllib.parse import urlsplit, urlunsplit

from codey.task.instruction_text import instruction_text, quoted_value
from codey.types.json_value import canonical_json_object

_CODE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|(?m:^[ \t]*>[^\n]*)")
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”')
_URL = re.compile(r"https?://[^\s<>`\"'、，。；！？（）\[\]]+", re.IGNORECASE)
_CLAUSES = re.compile(r"[，。；;!?！？]|\.\s+|\.$")
_URL_TOKEN = re.compile(r"\x00\d+\x00")
_URL_LINE = re.compile(r"[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?"
                       r"(?:(?:Source|URL|来源)[ \t]*[:：][ \t]*)?"
                       r"(?:<\x00\d+\x00>|\[[^\]\n]+\]\(\x00\d+\x00\)|\x00\d+\x00)[ \t。；.!?]*", re.I)
_ACTION = re.compile(
    r"web_search|open_url|\bbrowser_navigate\b|上网|联网(?:查询)?|在线查找|查网页|查资料|官方文档|浏览网页|读取网页|"
    r"\b(?:browse|search the web|look online)\b", re.IGNORECASE)
_OPEN = re.compile(r"打开|访问|读取|浏览|看看|查看|分析|了解|导航到|导航至|\b(?:open|visit|inspect|read|look at|navigate to)\b", re.IGNORECASE)
_NAVIGATION = re.compile(r"\bbrowser_navigate\b|导航到|导航至|\bnavigate to\b", re.IGNORECASE)
_NEGATED = re.compile(r"(?:不要|勿|禁止|不允许|无需|不需要|不用|不必|do not|don't|never|without)"
                      r"(?:\s|直接|使用|调用|用|再|去|帮我|请|directly\b|call\b)*$", re.IGNORECASE)
_DEFERRED_OPEN = re.compile(r"^(?:\s*(?:if|when|once|after)\b|\s*(?:如果|若|如需|必要时)|"
                            r"\s*(?:需要|待|等).{0,80}(?:时|后)\s*$)", re.IGNORECASE)


@dataclass(frozen=True)
class WebIntent:
    requested: bool
    urls: frozenset[str]
    original_urls: frozenset[str] = frozenset()
    browser_navigation_requested: bool = False
    deferred_urls: frozenset[str] = frozenset()


def explicit_browser_checks(task: str) -> tuple[str, ...]:
    """Admit explicit wait/read commands, without inferring authorization."""
    from codey.policies.tool_requirement import require_tool_success

    if len(explicit_web_intent(task).urls) > 1:
        return ()
    text = instruction_text(task, inline_names=frozenset({"browser_click", "browser_wait", "browser_read"}))
    checks: list[str] = []
    waiting = False
    pending: list[tuple[str, dict[str, object], int]] = []
    clicks = 0
    boundaries = [0, *(match.end() for match in re.finditer(r"[。；;\n]", text))]
    for start, end in zip(boundaries, (*boundaries[1:], len(text)), strict=True):
        clause = text[start:end]
        original = task[start:end]
        clause_start = 0
        for match in re.finditer(r"\bbrowser_(?:wait|read)\b", clause):
            before, after = clause[:match.start()], original[match.end():]
            after = after.lstrip("`")
            clicks += _click_mentions(clause[clause_start:match.start()])
            clause_start = match.end()
            if _NEGATED.search(before):
                continue
            command = re.search(r"(?:调用|使用|执行|再|最后|\bcall|\buse|\bthen)\s*$", before, re.I)
            predicate = re.match(r"\s*等待(?:页面)?(?:出现|显示)\s*", after)
            if command is None and predicate is None:
                continue
            if match[0] == "browser_wait":
                value = _wait_literal(after[predicate.end():]) if predicate else ""
                args: dict[str, object] = {"condition": "text", "value": value} if value else {}
                pending.append(("browser_wait", args, clicks))
                waiting = True
            elif waiting:
                pending.append(("browser_read", {}, clicks))
        clicks += _click_mentions(clause[clause_start:])
    for tool, arguments, stage in pending:
        checks.append(require_tool_success(tool, arguments=arguments,
            after="page_actions" if tool == "browser_wait" else "browser_wait", stage=stage))
    return tuple({check: None for check in checks})


def _wait_literal(text: str) -> str:
    text = text.strip()
    literal = quoted_value(text)
    if literal is not None:
        return literal
    boundary = re.search(r"(?:之后|以后|后)\s*(?=[，,。；;\n]|再|然后|调用|读取|$)|[，,。；;\n]|\b(?:then|before|after)\b", text)
    value = (text[:boundary.start()] if boundary is not None else text).strip()
    # An unquoted tool call or unmatched delimiter cannot be a literal predicate.
    return "" if re.search(r"[\"“”‘’「」『』`]|\bbrowser_\w+\b", value) else value


def _click_mentions(text: str) -> int:
    text = re.sub(r"browser_click\s*点击|点击\s*browser_click", "browser_click", text)
    return sum(_NEGATED.search(text[:match.start()]) is None
               for match in re.finditer(r"点击|\bbrowser_click\b", text))


def source_url_key(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        # User/tool input is validated by the acquisition network policy.
        return url
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _open_clause(clause: str) -> bool:
    return bool(_OPEN.search(clause) or re.search(r"\bopen_url\b", clause, re.I))


def _request_clauses(text: str, *, source_lines: bool) -> list[str]:
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
            if (0 <= line < len(lines) and clauses[line]
                    and (_open_clause(clauses[line][index]) or source_lines and _ACTION.search(clauses[line][index]))
                    and _URL_TOKEN.search(clauses[line][index]) is None):
                tokens = [match[0] for row in rows for match in _URL_TOKEN.finditer(lines[row])]
                clauses[line][index] += " " + " ".join(tokens)
                break
        else:
            # A neighboring open request already names its target. Its source
            # lines are background, rather than additional opening commands.
            if any(0 <= line < len(lines) and clauses[line]
                   and any(_NEGATED.search(clauses[line][index][:action.start()]) is None
                           for action in _OPEN.finditer(clauses[line][index]))
                   and _URL_TOKEN.search(clauses[line][index]) is not None
                   for line, index in targets):
                for row in rows:
                    clauses[row].clear()
                continue
            # A source address on its own line is task input even when the
            # surrounding question does not repeat an access verb.
            for row in rows:
                clauses[row] = [lines[row]]
            continue
        for row in rows:
            clauses[row].clear()
    return [clause for line in clauses for clause in line]


def explicit_web_intent(task: str, *, source_lines: bool = True) -> WebIntent:
    text = _CODE.sub("", task)

    def quoted_example(match: re.Match[str]) -> str:
        return "" if _OPEN.search(match[0]) or _ACTION.search(match[0]) or _NAVIGATION.search(match[0]) else match[0]

    text = _QUOTED.sub(quoted_example, text)
    url_tokens: dict[str, tuple[str, str | None]] = {}

    def protect_url(match: re.Match[str]) -> str:
        token = f"\x00{len(url_tokens)}\x00"
        value = match[0]
        address = value.rstrip(").,:;!?")
        # Markdown and an interior sentence boundary delimit the address.
        # A trailing punctuation mark without such a boundary stays ambiguous.
        markdown = value == address + ")" and re.search(r"\[[^\]\n]+\]\($", text[:match.start()]) is not None
        sentence = value == address + "." and re.match(r"[ \t]+[A-Z][a-z]+\b", text[match.end():]) is not None
        original = address if value == address or markdown or sentence else None
        url_tokens[token] = (source_url_key(address), original)
        return token + value[len(address):]

    text = _URL.sub(protect_url, text)
    requested = False
    browser_navigation_requested = False
    navigation_urls: set[str] = set()
    deferred_urls: set[str] = set()
    deferred_next = False
    direct: dict[str, set[str | None] | None] = {}
    for clause in _request_clauses(text, source_lines=source_lines):
        targets = list(_URL_TOKEN.finditer(clause))
        deferred = deferred_next or _DEFERRED_OPEN.search(clause) is not None
        deferred_next = not targets and _DEFERRED_OPEN.search(clause) is not None
        if source_lines and _URL_LINE.fullmatch(clause):
            for target in targets:
                key, original = url_tokens[target[0]]
                if key not in direct:
                    direct[key] = {original}
                    requested = True
                    if deferred:
                        deferred_urls.add(key)
            continue
        actions = list(_ACTION.finditer(clause))
        if targets:
            actions.extend(_OPEN.finditer(clause))
        actions.sort(key=lambda match: match.start())
        permissions = [(action, _NEGATED.search(clause[:action.start()]) is None) for action in actions]
        requested = requested or any(positive for _, positive in permissions)
        for index, target in enumerate(targets):
            if target[0] not in url_tokens or not permissions:
                continue
            preceding = [(action, positive) for action, positive in permissions if action.start() < target.start()]
            action, positive = preceding[-1] if preceding else permissions[0]
            key, original = url_tokens[target[0]]
            if not positive:
                direct[key] = None
                navigation_urls.discard(key)
                deferred_urls.discard(key)
                continue
            if deferred:
                deferred_urls.add(key)
            else:
                deferred_urls.discard(key)
            start = targets[index - 1].end() if index else 0
            if _NAVIGATION.fullmatch(action[0]) or any(allowed_step and step.start() >= start
                    and _NAVIGATION.fullmatch(step[0]) for step, allowed_step in preceding):
                navigation_urls.add(key)
            candidates = direct.get(key)
            if key in direct and candidates is None and (
                    not _open_clause(action[0]) or action[0] in {"分析", "了解"}):
                continue
            if candidates is None:
                direct[key] = {original}
            else:
                candidates.add(original)
        for navigation in _NAVIGATION.finditer(clause, targets[-1].end() if targets else 0):
            browser_navigation_requested = _NEGATED.search(clause[:navigation.start()]) is None
            if not browser_navigation_requested:
                navigation_urls.clear()
    allowed = {key: candidates for key, candidates in direct.items() if candidates is not None}
    originals = {url for candidates in allowed.values() for url in candidates if url is not None}
    unambiguous = all(None not in candidates for candidates in allowed.values())
    return WebIntent(requested, frozenset(allowed), frozenset(originals) if unambiguous else frozenset(),
                     browser_navigation_requested or bool(navigation_urls & allowed.keys()),
                     frozenset(deferred_urls & allowed.keys()))


def source_requirements(task: str) -> dict[str, object]:
    """Freeze explicit source requirements at task admission."""
    return {"version": 1, "required_urls": sorted(explicit_web_intent(task).urls)}


def restored_source_requirements(state: Mapping[str, object]) -> frozenset[str]:
    """Decode the entry snapshot; legacy tasks use their stored original input."""
    if "source_requirements" not in state:
        task = state.get("input_text", state.get("task", ""))
        if not isinstance(task, str):
            raise ValueError("original source requirement input must be text")
        return explicit_web_intent(task, source_lines=False).urls
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
