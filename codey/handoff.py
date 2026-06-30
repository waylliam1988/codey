"""Bounded, provider-neutral conversation handoff state."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Callable

DEFAULT_HARD_CONTEXT_TOKENS = 200_000
SOFT_CONTEXT_NUMERATOR = 3
SOFT_CONTEXT_DENOMINATOR = 4
MAX_HANDOFF_TEXT_CHARS = 2_000
MAX_HANDOFF_FILES = 20
MAX_MODEL_SUMMARY_CHARS = 6_000
MAX_SUMMARY_LIST_ITEMS = 12
SUMMARY_KEYS = (
    "goal",
    "decisions",
    "current_state",
    "next_step",
    "constraints",
    "open_questions",
)


def compact_text(value: str, limit: int = MAX_HANDOFF_TEXT_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def estimate_tokens(text: str) -> int:
    """Estimate tokens without provider-specific tokenizers."""

    value = str(text or "")
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    non_ascii_chars = len(value) - ascii_chars
    return (ascii_chars + 3) // 4 + non_ascii_chars


def _extract_json_object(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _normalize_model_summary(text: str) -> str:
    source = compact_text(text, MAX_MODEL_SUMMARY_CHARS)
    value = _extract_json_object(source)
    if value is None:
        value = {"current_state": source}

    summary: dict[str, object] = {}
    for key in SUMMARY_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            summary[key] = compact_text(item)
        elif isinstance(item, list):
            items = [
                compact_text(entry)
                for entry in item
                if isinstance(entry, str) and entry.strip()
            ][:MAX_SUMMARY_LIST_ITEMS]
            if items:
                summary[key] = items
    if not summary:
        summary["current_state"] = source
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class ConversationSnapshot:
    mode: str
    goal: str = ""
    project: str = ""
    provider_id: str = ""
    changed_files: tuple[str, ...] = ()
    checks_passed: bool | None = None
    summary: str = ""
    blocker: str = ""
    latest_user: str = ""
    latest_reply: str = ""
    conversation_summary: str = ""

    def to_payload(self) -> dict:
        payload: dict[str, object] = {
            "mode": self.mode,
            "goal": compact_text(self.goal),
        }
        if self.project:
            payload["project"] = self.project
        if self.provider_id:
            payload["previous_model"] = self.provider_id
        files = tuple(dict.fromkeys(self.changed_files))[:MAX_HANDOFF_FILES]
        if files:
            payload["changed_files"] = list(files)
        if self.checks_passed is not None:
            payload["checks"] = "passed" if self.checks_passed else "not passed"
        if self.summary:
            payload["latest_result"] = compact_text(self.summary)
        if self.blocker:
            payload["current_blocker"] = compact_text(self.blocker)
        if self.latest_user:
            payload["latest_user_message"] = compact_text(self.latest_user)
        if self.latest_reply:
            payload["latest_model_reply"] = compact_text(self.latest_reply)
        if self.conversation_summary:
            try:
                payload["conversation_summary"] = json.loads(self.conversation_summary)
            except json.JSONDecodeError:
                payload["conversation_summary"] = compact_text(
                    self.conversation_summary,
                    MAX_MODEL_SUMMARY_CHARS,
                )
        return {key: value for key, value in payload.items() if value not in ("", [], None)}


def render_handoff(snapshot: ConversationSnapshot) -> str:
    """Serialize only the latest factual snapshot, never prior handoffs."""

    return json.dumps(snapshot.to_payload(), ensure_ascii=False, indent=2)


def render_continuation_prompt(handoff: str, current_request: str) -> str:
    return (
        "Continue the conversation seamlessly using the factual handoff below. "
        "Do not mention the handoff, context limits, or that a new model chat was opened.\n\n"
        f"Factual handoff:\n{handoff}\n\n"
        f"Current request:\n{current_request}"
    )


def render_summary_prompt(snapshot: ConversationSnapshot) -> str:
    known_facts = render_handoff(replace(snapshot, conversation_summary=""))
    return (
        "Codey will continue this conversation in a fresh model chat. "
        "Do not call tools and do not continue the task yet. Return only one compact "
        "JSON object with these optional keys: goal, decisions, current_state, "
        "next_step, constraints, open_questions. Keep only facts needed to continue. "
        "Do not include greetings, repeated tool output, code bodies, or any mention "
        "of context limits or handoff mechanics.\n\n"
        f"Known local facts:\n{known_facts}"
    )


@dataclass
class ConversationContext:
    """Small in-memory state for one Codey conversation."""

    hard_limit: int = DEFAULT_HARD_CONTEXT_TOKENS
    used_tokens: int = 0
    provider_id: str = ""
    mode: str = ""
    project: str = ""
    initialized: bool = False
    handoff_summary: str = ""
    snapshot: ConversationSnapshot = field(
        default_factory=lambda: ConversationSnapshot(mode="chat")
    )
    on_change: Callable[[ConversationContext], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def _changed(self) -> None:
        callback = self.on_change
        if callback is not None:
            callback(self)

    @property
    def soft_limit(self) -> int:
        return max(
            1,
            self.hard_limit * SOFT_CONTEXT_NUMERATOR // SOFT_CONTEXT_DENOMINATOR,
        )

    def clear(self) -> None:
        self.used_tokens = 0
        self.provider_id = ""
        self.mode = ""
        self.project = ""
        self.initialized = False
        self.handoff_summary = ""
        self.snapshot = ConversationSnapshot(mode="chat")
        self._changed()

    def begin_window(self, provider_id: str, mode: str, project: str = "") -> None:
        self.provider_id = provider_id
        self.mode = mode
        self.project = project
        self.used_tokens = 0
        self.initialized = True
        self.handoff_summary = ""
        self.snapshot = replace(self.snapshot, conversation_summary="")
        self._changed()

    def prepare_handoff(self) -> str:
        self.handoff_summary = render_handoff(self.snapshot)
        self._changed()
        return self.handoff_summary

    def prepare_model_handoff(self, send_summary: Callable[[str], str]) -> str:
        if self.snapshot.conversation_summary:
            return self.prepare_handoff()
        prompt = render_summary_prompt(self.snapshot)
        try:
            reply = send_summary(prompt)
        except Exception:
            return self.prepare_handoff()
        self.used_tokens += estimate_tokens(prompt) + estimate_tokens(reply)
        self.snapshot = replace(
            self.snapshot,
            conversation_summary=_normalize_model_summary(reply),
        )
        return self.prepare_handoff()

    def update_snapshot(self, snapshot: ConversationSnapshot) -> None:
        self.snapshot = snapshot
        if self.used_tokens >= self.soft_limit:
            self.prepare_handoff()
        else:
            self._changed()

    def record_exchange(
        self,
        prompt: str,
        reply: str,
        snapshot: ConversationSnapshot | None = None,
    ) -> None:
        self.used_tokens += estimate_tokens(prompt) + estimate_tokens(reply)
        if snapshot is not None:
            self.snapshot = snapshot
        if self.used_tokens >= self.soft_limit:
            self.prepare_handoff()
        else:
            self._changed()

    def needs_rollover(self, next_prompt: str = "") -> bool:
        return (
            self.initialized
            and self.used_tokens + estimate_tokens(next_prompt) >= self.soft_limit
        )

    def plan_request(
        self,
        *,
        provider_id: str,
        mode: str,
        project: str = "",
        force_rollover: bool = False,
        next_prompt: str = "",
    ) -> tuple[bool, str]:
        if not self.initialized:
            return True, ""
        if self.mode != mode or self.project != project:
            self.clear()
            return True, ""
        if (
            force_rollover
            or provider_id != self.provider_id
            or self.needs_rollover(next_prompt)
        ):
            return True, self.prepare_handoff()
        return False, ""
