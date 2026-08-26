"""JSON contract for Ghost signal extraction."""

from __future__ import annotations

import json
from typing import Any

from codey.ghost.schema import (
    MAX_EXTRACTOR_ASSISTANT_CHARS,
    MAX_EXTRACTOR_USER_CHARS,
    GhostSignalParseResult,
    signals_from_payload,
)
from codey.ghost.typed_fields import extractor_metadata_guidance
from codey.utils.text_budget import clip_middle


PROTOCOL_NO_JSON = "no_json"
PROTOCOL_TOO_MANY_JSON = "too_many_json"
PROTOCOL_INVALID_SCHEMA = "invalid_schema"


class GhostSignalCodec:
    """Small provider-neutral JSON codec for explicit learning signals."""

    def system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def format_request(
        self,
        *,
        user_text: str,
        assistant_text: str = "",
        context: str = "",
    ) -> str:
        user, user_truncated = clip_middle(str(user_text or ""), MAX_EXTRACTOR_USER_CHARS)
        assistant, assistant_truncated = clip_middle(
            str(assistant_text or ""),
            MAX_EXTRACTOR_ASSISTANT_CHARS,
        )
        context_block = f"\nContext:\n{context.strip()}\n" if context.strip() else ""
        assistant_block = (
            f"\nAssistant reply context (do not quote this as evidence):\n{assistant}\n"
            if assistant.strip()
            else ""
        )
        truncation_note = ""
        if user_truncated or assistant_truncated:
            truncation_note = "\nSome input was clipped; use only visible user text as evidence.\n"
        return (
            f"{self.system_prompt()}\n"
            f"{context_block}"
            f"\nUser message:\n{user}\n"
            f"{assistant_block}"
            f"{truncation_note}"
            "\nReturn exactly one JSON object now."
        )

    def parse(
        self,
        reply: str,
        *,
        user_text: str,
        provider_id: str = "",
    ) -> GhostSignalParseResult:
        objects = extract_json_objects(reply or "")
        if not objects:
            return GhostSignalParseResult(
                diagnostics=(PROTOCOL_NO_JSON,),
                ok=False,
                raw_text_chars=len(reply or ""),
                provider_id=provider_id,
            )
        if len(objects) > 1:
            return GhostSignalParseResult(
                diagnostics=(f"{PROTOCOL_TOO_MANY_JSON}: {len(objects)}",),
                ok=False,
                raw_text_chars=len(reply or ""),
                provider_id=provider_id,
            )
        signals, diagnostics = signals_from_payload(objects[0], user_text=user_text)
        return GhostSignalParseResult(
            signals=signals,
            diagnostics=diagnostics,
            ok=not diagnostics,
            raw_text_chars=len(reply or ""),
            provider_id=provider_id,
        )

    def public_example(self) -> str:
        return json.dumps(
            {
                "signals": [{
                    "kind": "style_preference",
                    "scope": "user",
                    "summary": "Prefer answers that start with the conclusion.",
                    "evidence_quote": "以后请先给结论",
                    "confidence": 0.92,
                }],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    in_string = False
    escaped = False
    start: int | None = None
    depth = 0

    for index, char in enumerate(str(text or "")):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                raw = text[start : index + 1]
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    start = None
                    continue
                if isinstance(value, dict):
                    objects.append(value)
                start = None
    return objects


_SYSTEM_PROMPT = f"""\
You extract explicit learning signals for a local assistant.

Your job is narrow: decide whether the user's current message contains an
explicit learning signal that the assistant should consider remembering later.

Accepted signal kinds:
- style_preference: how the user wants the assistant to communicate.
- correction: the user explicitly corrects something the assistant said or assumed.
- research_interest: the user marks a topic as worth tracking or revisiting.
- long_term_goal: the user states an enduring project or personal goal.
- action_tendency: the user states a preferred way the assistant should act next time.

Rules:
- Do not infer memory from ordinary conversation, thanks, continuation, or task
  instructions unless the user explicitly asks to remember a preference,
  correction, goal, research interest, or future action tendency.
- evidence_quote must be a short exact quote from the user message, not from
  assistant context.
- If there is no explicit signal, return {{"signals":[]}}.
- Use correction only when the user explicitly says the assistant was wrong, mistaken,
  or gives a replacement truth, such as "你刚才说错了，正确是...".
- Use style_preference for communication format, tone, length, ordering, or
  wording preferences, such as "先给结论", "短一点", or "不要营销味".
- If one style_preference message contains multiple allowed metadata fields,
  return one signal per field, such as one for "shorter" and one for "answer first".
- Use action_tendency for workflow/process behavior, such as searching,
  verifying evidence, reading files, testing, or checking sources before future
  answers or actions.
- Use action_tendency, not correction, when the user says how the assistant
  should act in future, such as "以后先查证据再回答".
- Return exactly one JSON object and no prose.
- Return at most five signals.
- scope must be one of: user, project, session.
- {extractor_metadata_guidance()}

JSON shape:
{{"signals":[{{"kind":"style_preference","scope":"user","summary":"...","evidence_quote":"...","confidence":0.0,"metadata":{{"conflict_key":"reply_length","value_key":"concise"}}}}]}}
"""
