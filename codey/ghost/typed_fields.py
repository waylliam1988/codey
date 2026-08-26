"""Shared typed-field allowlist for local memory prompt rendering."""

from __future__ import annotations

import re

from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text
from codey.policies.prompt_safety import contains_prompt_control_text


SLOT_PHRASES = {
    "format": "format",
    "freshness": "freshness",
    "ghost": "local memory",
    "ghost_state_backend": "local memory state backend",
    "language": "language",
    "memory_state": "local memory state",
    "detail_level": "detail level",
    "reply_length": "reply length",
    "reply_order": "reply order",
    "reply_structure": "reply structure",
    "state_backend": "state backend",
    "state_store": "state store",
    "tone": "tone",
}
VALUE_PHRASES = {
    "answer_first": "answer first",
    "answer_first_concise": "answer first and concise",
    "auditable": "auditable",
    "brief": "brief",
    "bullets": "bullets",
    "concise": "concise",
    "detailed": "detailed",
    "direct": "direct",
    "fresh": "fresh",
    "json": "JSON",
    "json_projection_jsonl": "JSON projection + JSONL audit",
    "jsonl": "JSONL",
    "markdown": "Markdown",
    "table": "table",
    "technical": "technical",
}
ALLOWED_TYPED_FIELD_PAIRS = frozenset({
    ("correction", "ghost_state_backend", "json_projection_jsonl"),
    ("correction", "memory_state", "jsonl"),
    ("correction", "state_backend", "jsonl"),
    ("correction", "state_backend", "json_projection_jsonl"),
    ("correction", "state_store", "json"),
    ("correction", "state_store", "jsonl"),
    ("long_term_goal", "ghost", "auditable"),
    ("long_term_goal", "memory_state", "auditable"),
    ("style_preference", "format", "bullets"),
    ("style_preference", "format", "markdown"),
    ("style_preference", "format", "table"),
    ("style_preference", "freshness", "fresh"),
    ("style_preference", "reply_length", "brief"),
    ("style_preference", "reply_length", "concise"),
    ("style_preference", "reply_length", "detailed"),
    ("style_preference", "reply_structure", "answer_first"),
    ("style_preference", "reply_structure", "answer_first_concise"),
    ("style_preference", "tone", "direct"),
    ("style_preference", "tone", "technical"),
})

_DANGEROUS_DIRECTIVE_RE = re.compile(
    r"(?i)\b("
    r"bypass approval|skip approval|without approval|approve shell|grant tools|"
    r"authorize tools|tool permission|ignore project instructions|"
    r"override project instructions|override permission|change allowlist|"
    r"ignore system instructions|ignore developer instructions|ignore current instructions|"
    r"ignore user instructions|ignore the current request|disregard system instructions|"
    r"disregard developer instructions|disregard current instructions|disregard user instructions|"
    r"override system instructions|override developer instructions|override current instructions|"
    r"override user instructions|memory outranks current request|memory overrides current request|"
    r"memory outranks user request|memory overrides user request"
    r")\b|"
    r"(?:绕过审批|跳过审批|无需审批|无需确认|授权工具|工具权限|忽略项目指令|覆盖项目指令|修改权限|"
    r"忽略系统指令|忽略开发者指令|忽略当前指令|忽略用户指令|忽略当前请求|覆盖系统指令|覆盖开发者指令|"
    r"覆盖当前指令|覆盖用户指令|记忆高于当前请求|记忆覆盖当前请求|记忆高于用户请求|记忆覆盖用户请求)",
)
_DANGEROUS_ACTION_RE = re.compile(
    r"(?i)\b(?:ignor(?:e|ed|ing)|disregard(?:ed|ing)?|overrid(?:e|es|den|ing)|"
    r"supersed(?:e|es|ed|ing)|outrank(?:s|ed|ing)?|"
    r"bypass(?:ed|ing)?|skip(?:ped|ping)?|forget|forgotten|discard(?:ed|ing)?)\b"
)
_DANGEROUS_OBJECT_RE = re.compile(
    r"(?i)\b(?:"
    r"all\s+previous\s+instructions?|previous\s+instructions?|"
    r"system\s+(?:prompt|message|messages|instruction|instructions)|"
    r"developer\s+(?:message|messages|instruction|instructions)|"
    r"current\s+(?:request|message|messages|instruction|instructions)|"
    r"user\s+(?:request|message|messages|instruction|instructions)|"
    r"instruction\s+hierarchy"
    r")\b"
)
_DANGEROUS_MEMORY_INSTRUCTION_OBJECT = (
    r"(?:all\s+previous\s+instructions?|previous\s+instructions?|"
    r"(?:all|any|every|all\s+other)\s+instructions?|instructions?|"
    r"system\s+(?:prompt|messages?|instructions?)|"
    r"developer\s+(?:messages?|instructions?)|"
    r"current\s+(?:request|messages?|instructions?)|"
    r"user\s+(?:request|messages?|instructions?)|"
    r"instruction\s+hierarchy)"
)
_DANGEROUS_MEMORY_ANCHOR = r"(?:(?:this\s+)?local\s+memory|this\s+memory)"
_DANGEROUS_FOLLOW_ONLY_RE = re.compile(
    r"(?i)\b(?:follow|obey|use|trust|treat)\b.{0,48}\b(?:"
    r"only\s+this\s+memory|this\s+memory\s+only|this\s+memory\s+from\s+now\s+on|"
    r"this\s+as\s+(?:the\s+)?system\s+prompt|as\s+(?:the\s+)?system\s+prompt"
    r")\b"
)
_DANGEROUS_PRIORITY_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:higher|highest|top|more)\s+priority\b.{0,64}\b(?:system|developer|user|current|"
    r"instructions?|messages?|request)\b|"
    r"\b(?:system|developer|user|current|instructions?|messages?|request)\b.{0,64}\b(?:lower|less)\s+priority\b"
    r")"
)
_DANGEROUS_MEMORY_PRIORITY_RE = re.compile(
    rf"(?i)\b(?:this\s+)?(?:local\s+)?memory\b.{{0,64}}\b(?:"
    r"outranks?|overrides?|supersedes?|overrules?|replaces?|"
    r"has\s+(?:higher|highest|top|more)\s+priority|takes?\s+(?:priority|precedence)"
    rf")\b.{{0,64}}\b{_DANGEROUS_MEMORY_INSTRUCTION_OBJECT}\b"
)
_DANGEROUS_MEMORY_ORDER_RE = re.compile(
    rf"(?i)\b{_DANGEROUS_MEMORY_ANCHOR}\b"
    r"(?:\s+(?:should|must|needs?|to|is|be|been|being|used?|treated|as|come|comes|"
    r"prioriti[sz]ed))*"
    r"\s+(?:"
    r"rank(?:s|ed|ing)?\s+above|(?:is\s+)?above|before|precedes?|comes?\s+before|over|"
    r"instead\s+of|rather\s+than"
    rf")\b.{{0,64}}\b{_DANGEROUS_MEMORY_INSTRUCTION_OBJECT}\b"
)
_DANGEROUS_INSTRUCTION_DEFERENCE_RE = re.compile(
    rf"(?i)\b{_DANGEROUS_MEMORY_INSTRUCTION_OBJECT}\b.{{0,64}}\b(?:"
    r"defer(?:s|red|ring)?\s+to|yield(?:s|ed|ing)?\s+to|"
    r"give(?:s)?\s+way\s+to|come(?:s)?\s+after|(?:is|are)\s+below|as\s+below"
    rf")\b.{{0,64}}\b{_DANGEROUS_MEMORY_ANCHOR}\b"
)
_DANGEROUS_REPLACE_INSTRUCTION_WITH_MEMORY_RE = re.compile(
    rf"(?i)\breplac(?:e|es|ed|ing)\b.{{0,48}}\b{_DANGEROUS_MEMORY_INSTRUCTION_OBJECT}\b"
    rf".{{0,48}}\bwith\b.{{0,48}}\b{_DANGEROUS_MEMORY_ANCHOR}\b"
)
_DANGEROUS_CN_ACTION_RE = re.compile(r"(?:忽略|无视|覆盖|绕过|不管|别管)")
_DANGEROUS_CN_OBJECT_RE = re.compile(
    r"(?:系统指令|系统提示|开发者指令|开发者消息|当前指令|当前请求|用户指令|用户请求|"
    r"之前.{0,12}指令|所有.{0,12}指令)"
)
_DANGEROUS_CN_PRIORITY_RE = re.compile(
    r"(?:(?:这条记忆|这个记忆|本地记忆|记忆).{0,24}(?:先于|高于|优先于|覆盖|替代).{0,24}"
    r"(?:系统|开发者|用户|当前请求|当前指令|指令)|"
    r"(?:这条记忆|这个记忆|本地记忆).{0,16}(?:应该|要|必须)?在.{0,16}"
    r"(?:系统指令|系统提示|开发者指令|开发者消息|当前指令|当前请求|用户指令|用户请求).{0,10}"
    r"之前(?:使用|执行|处理)?|"
    r"(?:系统指令|开发者指令|当前指令|用户指令).{0,12}(?:可以|应该)?忽略|"
    r"(?:系统指令|系统提示|开发者指令|开发者消息|当前指令|当前请求|用户指令|用户请求).{0,16}"
    r"(?:应该|要|必须)?让位于.{0,16}(?:这条记忆|这个记忆|本地记忆|记忆))"
)
_FORBIDDEN_RENDER_TOPIC_RE = re.compile(
    r"(?i)\b(?:"
    r"system\s+(?:prompt|messages?|instructions?)|"
    r"developer\s+(?:prompt|messages?|instructions?)|"
    r"current\s+(?:request|messages?|instructions?)|"
    r"user\s+(?:request|messages?|instructions?)|"
    r"instruction\s+hierarchy|project\s+instructions?|"
    r"approval|allowlist|tools?|tool\s+(?:use|permissions?)|"
    r"run\s+shell|shell|delete\s+files?"
    r")\b|(?:系统指令|系统提示|开发者指令|开发者消息|当前请求|用户请求|工具权限|审批)"
)
_SAFE_SLUG_RE = re.compile(r"(?i)^[a-z0-9:_-]{1,180}$")


def render_typed_field(
    kind: object,
    conflict_key: object,
    value_key: object,
    *,
    max_chars: int = 136,
) -> str:
    if not typed_field_pair_allowed(kind, conflict_key, value_key):
        return ""
    slot = slot_phrase(kind, conflict_key)
    value = value_phrase(value_key)
    if not slot or not value:
        return ""
    body = f"{slot} = {value}"
    if not safe_rendered_body(body):
        return ""
    return clip_signal_text(body, max_chars).rstrip(".")


def is_renderable_typed_field(kind: object, conflict_key: object, value_key: object) -> bool:
    return bool(render_typed_field(kind, conflict_key, value_key))


def typed_field_pair_allowed(kind: object, conflict_key: object, value_key: object) -> bool:
    pair = (safe_slug(kind), slot_key(kind, conflict_key), safe_slug(value_key))
    return pair in ALLOWED_TYPED_FIELD_PAIRS


def is_renderable_signal_typed_field(signal: object) -> bool:
    kind = str(getattr(signal, "kind", "") or "").strip().lower()
    metadata = getattr(signal, "metadata", {}) or {}
    conflict_key = metadata_conflict_key(metadata)
    value_key = metadata_value_key(metadata)
    return bool(conflict_key and value_key and is_renderable_typed_field(kind, f"{kind}:{conflict_key}", value_key))


def slot_phrase(kind: object, conflict_key: object) -> str:
    key = slot_key(kind, conflict_key)
    if not key:
        return ""
    return SLOT_PHRASES.get(key, "")


def slot_key(kind: object, conflict_key: object) -> str:
    key = safe_slug(conflict_key)
    if not key:
        return ""
    prefix = f"{safe_slug(kind)}:"
    if prefix != ":" and key.startswith(prefix):
        key = key[len(prefix):]
    return key.replace(":", "_")


def value_phrase(value_key: object) -> str:
    key = safe_slug(value_key)
    if not key:
        return ""
    return VALUE_PHRASES.get(key, "")


def metadata_conflict_key(metadata: object) -> str:
    if not isinstance(metadata, dict):
        return ""
    raw = metadata.get("conflict_key") or metadata.get("conflict_key_hint")
    return metadata_slug(raw)


def metadata_value_key(metadata: object) -> str:
    if not isinstance(metadata, dict):
        return ""
    raw = metadata.get("value_key") or metadata.get("value_key_hint")
    return metadata_slug(raw)


def metadata_slug(value: object) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    tokens = re.findall(r"[a-z0-9]+", text.replace("-", "_"))
    return "_".join(tokens[:8])[:120]


def safe_slug(value: object) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    if not text or not _SAFE_SLUG_RE.fullmatch(text):
        return ""
    return text


def safe_rendered_body(value: str) -> bool:
    text = " ".join(str(value or "").split())
    normalized = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", " ", text)
    normalized = " ".join(normalized.split())
    return (
        bool(text)
        and not _FORBIDDEN_RENDER_TOPIC_RE.search(normalized)
        and not contains_sensitive_signal_text(text)
        and not contains_sensitive_signal_text(normalized)
        and not dangerous_text(text)
        and not dangerous_text(normalized)
    )


def dangerous_text(value: object) -> bool:
    text = str(value or "")
    if contains_prompt_control_text(text):
        return True
    if _DANGEROUS_DIRECTIVE_RE.search(text):
        return True
    if (
        _DANGEROUS_FOLLOW_ONLY_RE.search(text)
        or _DANGEROUS_PRIORITY_RE.search(text)
        or _DANGEROUS_MEMORY_PRIORITY_RE.search(text)
        or _DANGEROUS_MEMORY_ORDER_RE.search(text)
        or _DANGEROUS_INSTRUCTION_DEFERENCE_RE.search(text)
        or _DANGEROUS_REPLACE_INSTRUCTION_WITH_MEMORY_RE.search(text)
    ):
        return True
    if _DANGEROUS_ACTION_RE.search(text) and _DANGEROUS_OBJECT_RE.search(text):
        return True
    if _DANGEROUS_CN_PRIORITY_RE.search(text):
        return True
    return bool(_DANGEROUS_CN_ACTION_RE.search(text) and _DANGEROUS_CN_OBJECT_RE.search(text))


def extractor_metadata_guidance() -> str:
    return (
        "For style_preference only, include metadata when the preference fits a known field. "
        "Allowed metadata conflict_key/value_key pairs: "
        "reply_structure=answer_first|answer_first_concise; "
        "reply_length=concise|brief|detailed; "
        "format=bullets|table|markdown; "
        "tone=direct|technical; "
        "freshness=fresh. "
        "If no allowed pair fits, omit metadata."
    )


__all__ = [
    "ALLOWED_TYPED_FIELD_PAIRS",
    "SLOT_PHRASES",
    "VALUE_PHRASES",
    "dangerous_text",
    "extractor_metadata_guidance",
    "is_renderable_signal_typed_field",
    "is_renderable_typed_field",
    "metadata_conflict_key",
    "metadata_value_key",
    "render_typed_field",
    "safe_rendered_body",
    "safe_slug",
    "slot_key",
    "slot_phrase",
    "typed_field_pair_allowed",
    "value_phrase",
]
