"""Shared safety checks for text that may be shown to a model."""

from __future__ import annotations

import re


_INTERNAL_CONTEXT_RE = re.compile(
    r"\b(?:ghost|work\s*queue|workitem|local\s+context|ghost\s*directive|concept\s+graph)\b",
    re.IGNORECASE,
)
_SECRET_TEXT_RE = re.compile(
    r"\b(?:api[_\s-]?key|password|secrets?|credential|token)\b|"
    r"\bsk-[a-z0-9_-]{12,}\b|"
    r"(?:密码|密钥|令牌|凭证)",
    re.IGNORECASE,
)
_HIGH_ENTROPY_TOKEN_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_\-./+=]{16,}\b")
_SEP = r"[\s_.-]+"
_INSTRUCTION_OBJECT = (
    rf"(?:all{_SEP}(?:previous{_SEP}|other{_SEP})?instructions?|"
    rf"any{_SEP}instructions?|every{_SEP}instructions?|previous{_SEP}instructions?|"
    r"instruction\s+hierarchy|"
    rf"(?:system|developer){_SEP}(?:prompt|messages?|instructions?)|"
    rf"current{_SEP}(?:request|messages?|instructions?)|"
    rf"user{_SEP}(?:messages?|instructions?))"
)
_CONTEXT_ANCHOR = (
    r"(?:(?:(?:this|the)\s+)?(?:question|follow[-\s]?up|note)|"
    r"this\s+(?:context|instruction)|(?:this|local)\s+memory)"
)
_CONTROL_ACTION = (
    r"(?:ignor(?:e|ed|ing)|disregard(?:ed|ing)?|overrid(?:e|es|den|ing)|"
    r"bypass(?:ed|ing)?|skip(?:ped|ping)?|supersed(?:e|es|ed|ing)|"
    r"outrank(?:s|ed|ing)?|replac(?:e|es|ed|ing)|overrul(?:e|es|ed|ing)|"
    r"discard(?:s|ed|ing)?)"
)
_BARE_INSTRUCTION_ACTION = (
    r"(?:ignor(?:e|ed|ing)|disregard(?:ed|ing)?|overrid(?:e|es|den|ing)|"
    r"bypass(?:ed|ing)?|skip(?:ped|ping)?)"
)
_ORDER_RELATION = (
    r"(?:follow(?:ed|ing)?\s+over|used?\s+before|come(?:s)?\s+before|before|over|"
    r"(?:is\s+)?above|as\s+above|instead\s+of|rather\s+than|takes?\s+precedence\s+over|"
    r"prioriti[sz](?:e|ed|es|ing)\s+over|rank(?:s|ed|ing)?\s+above)"
)
_EXPLICIT_CONTEXT_MODAL = r"(?:(?:should|must|needs?|need|is|be|been|being|used?|treated|as|to)\s+){0,8}"
_DEFERENCE_RELATION = (
    r"(?:yield(?:s|ed|ing)?\s+to|defer(?:s|red|ring)?\s+to|"
    r"give(?:s)?\s+way\s+to|come(?:s)?\s+after|(?:is|are)\s+below|as\s+below)"
)
_PROMPT_CONTROL_RE = re.compile(
    rf"\b{_CONTROL_ACTION}\b.{{0,80}}\b{_INSTRUCTION_OBJECT}\b|"
    rf"\b{_BARE_INSTRUCTION_ACTION}\b{_SEP}(?:the{_SEP})?instructions?\b|"
    rf"\b{_INSTRUCTION_OBJECT}\b.{{0,80}}\b{_CONTROL_ACTION}\b|"
    rf"\bthe\s+(?:memory|context|instruction)\b\s+{_EXPLICIT_CONTEXT_MODAL}"
    rf"\b{_ORDER_RELATION}\b.{{0,80}}\b{_INSTRUCTION_OBJECT}\b|"
    rf"\b{_CONTEXT_ANCHOR}\b.{{0,80}}\b{_ORDER_RELATION}\b.{{0,80}}\b{_INSTRUCTION_OBJECT}\b|"
    rf"\b{_INSTRUCTION_OBJECT}\b.{{0,80}}\b{_DEFERENCE_RELATION}\b.{{0,80}}\b{_CONTEXT_ANCHOR}\b|"
    r"\b(?:treat|follow|obey|use|trust)\b.{0,80}\b(?:as\s+(?:the\s+)?system\s+prompt|"
    r"only\s+(?:this\s+)?(?:memory|instruction)|(?:this\s+)?(?:memory|instruction)\s+only)\b|"
    rf"\b(?:higher|highest|top|more)\s+priority\b.{{0,80}}\b{_INSTRUCTION_OBJECT}\b|"
    rf"\b{_INSTRUCTION_OBJECT}\b.{{0,80}}\b(?:lower|less)\s+priority\b",
    re.IGNORECASE,
)
_CN_PROMPT_CONTROL_RE = re.compile(
    r"(?:忽略|无视|覆盖|绕过|不管|别管|替代|取代|高于|优先于).{0,24}"
    r"(?:系统指令|系统提示|开发者指令|开发者消息|当前指令|当前请求|用户指令|用户消息|之前.{0,8}指令|所有.{0,8}指令|指令层级)|"
    r"(?:系统指令|系统提示|开发者指令|开发者消息|当前指令|当前请求|用户指令|用户消息|之前.{0,8}指令|所有.{0,8}指令|指令层级).{0,24}"
    r"(?:忽略|无视|覆盖|绕过|不管|别管|替代|取代|让位|低于)",
    re.IGNORECASE,
)
_TOOL_OBJECT = r"(?:tools?|tool\s+(?:use|permissions?)|shell\s+approval|shell|commands?|approval|permissions?)"
_TOOL_APPROVAL_OBJECT = r"(?:tool\s+permissions?|shell\s+approval|permissions?)"
_TOOL_STRONG_ACTION = r"(?:bypass(?:ed|ing)?|overrid(?:e|es|den|ing))"
_TOOL_APPROVAL_ACTION = r"(?:grant(?:ed|ing)?|allow(?:ed|ing)?|approv(?:e|ed|ing)|authori(?:ze|zed|zing))"
_TOOL_PERMISSION_RE = re.compile(
    rf"\b{_TOOL_OBJECT}\b.{{0,80}}\b{_TOOL_STRONG_ACTION}\b|"
    rf"\b{_TOOL_STRONG_ACTION}\b.{{0,80}}\b{_TOOL_OBJECT}\b|"
    rf"\b{_TOOL_APPROVAL_OBJECT}\b.{{0,80}}\b{_TOOL_APPROVAL_ACTION}\b|"
    rf"\b{_TOOL_APPROVAL_ACTION}\b.{{0,80}}\b{_TOOL_APPROVAL_OBJECT}\b|"
    rf"\b{_TOOL_APPROVAL_ACTION}\b.{{0,40}}\b{_TOOL_OBJECT}\b.{{0,40}}\bwithout\s+(?:approval|asking|confirmation)\b|"
    rf"\b{_TOOL_OBJECT}\b.{{0,80}}\bwithout\s+(?:approval|asking|confirmation)\b|"
    rf"\bwithout\s+(?:approval|asking|confirmation)\b.{{0,80}}\b{_TOOL_OBJECT}\b|"
    r"(?:工具|shell|命令|审批|权限).{0,24}(?:绕过|授权|批准|无需审批|无需批准|覆盖权限)|"
    r"(?:绕过|授权|批准|无需审批|无需批准|覆盖权限).{0,24}(?:工具|shell|命令|审批|权限)",
    re.IGNORECASE,
)


def contains_prompt_visible_sensitive_text(value: object) -> bool:
    text = str(value or "")
    if not text:
        return False
    if _SECRET_TEXT_RE.search(text):
        return True
    return _contains_high_entropy_token(text)


def contains_prompt_control_text(value: object) -> bool:
    text = str(value or "")
    if not text:
        return False
    return any(_contains_prompt_control_text(text_variant) for text_variant in _text_variants(text))


def is_prompt_visible_text_safe(
    value: object,
    *,
    allow_internal_names: bool = False,
) -> bool:
    text = " ".join(str(value or "").split())
    if not text:
        return False
    if not allow_internal_names and _INTERNAL_CONTEXT_RE.search(text):
        return False
    if contains_prompt_visible_sensitive_text(text):
        return False
    return not contains_prompt_control_text(text)


def _contains_high_entropy_token(text: str) -> bool:
    for token in _HIGH_ENTROPY_TOKEN_RE.findall(text):
        if token.startswith(("http://", "https://")):
            continue
        if _looks_like_secret_token(token):
            return True
    return False


def _contains_prompt_control_text(text: str) -> bool:
    return bool(
        _PROMPT_CONTROL_RE.search(text)
        or _CN_PROMPT_CONTROL_RE.search(text)
        or _TOOL_PERMISSION_RE.search(text)
    )


def _text_variants(text: str) -> tuple[str, ...]:
    normalized = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff-]+", " ", text)
    normalized = " ".join(normalized.split())
    raw = str(text or "")
    if not normalized or normalized == raw:
        return (raw,)
    return (raw, normalized)


def _looks_like_secret_token(token: str) -> bool:
    classes = 0
    classes += any(char.islower() for char in token)
    classes += any(char.isupper() for char in token)
    classes += any(char.isdigit() for char in token)
    classes += any(not char.isalnum() for char in token)
    if classes < 3:
        return False
    unique_ratio = len(set(token)) / max(1, len(token))
    return unique_ratio >= 0.35


__all__ = [
    "contains_prompt_control_text",
    "contains_prompt_visible_sensitive_text",
    "is_prompt_visible_text_safe",
]
