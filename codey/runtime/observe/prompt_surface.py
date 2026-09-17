"""Thin prompt surface summary helpers.

PromptSurfaceRecord is a per provider-send attempt row: one outbound
attempt -> one surface_id. prompt_digest is the content identity, surface_id
is the send identity. The trace stores only digests, sizes, refs and contract
hashes -- never raw prompt/reply/source bodies.

Pure data helpers only. No imports of providers, agents, or ghost.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping

PROMPT_SURFACE_SCHEMA_VERSION = 1

_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "prompt",
        "reply",
        "content",
        "body",
        "text",
        "stdout",
        "stderr",
        "diff",
        "source_body",
        "result",
        "model_text",
    }
)

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_EPOCH_RE = re.compile(r"ctx_epoch:[0-9a-f]{16}")
_SURFACE_RE = re.compile(r"prompt_surface:[0-9a-f]{16}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._:-]+")
_SEND_REF_RE = re.compile(r"[A-Za-z0-9._:-]{1,80}")


@dataclass(frozen=True)
class PromptSurfaceSection:
    name: str
    digest: str
    chars: int
    source_refs: tuple[str, ...] = ()
    model_visible: bool = True
    freshness: str = ""
    epoch_id: str = ""
    capability_id: str = ""


@dataclass(frozen=True)
class PromptSurfaceRecord:
    surface_id: str
    phase: str
    prompt_digest: str
    prompt_chars: int
    epoch_id: str
    send_ref: str = ""
    source_refs: tuple[str, ...] = ()
    provider_effect_id: str = ""
    model_tool_contract_hash: str = ""
    runtime_tool_contract_hash: str = ""
    sections: tuple[PromptSurfaceSection, ...] = ()
    schema_version: int = PROMPT_SURFACE_SCHEMA_VERSION


def prompt_surface_id(*, phase: str, send_ref: str, prompt_digest: str) -> str:
    raw = f"{phase}\0{send_ref}\0{prompt_digest}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"prompt_surface:{digest}"


def canonical_surface_phase(value: object) -> str:
    return str(value or "").strip()


def canonical_surface_send_ref(value: object) -> str:
    return str(value or "").strip()


def canonical_surface_prompt_digest(value: object) -> str:
    return str(value or "").strip()


def build_prompt_surface_record(
    *,
    phase: str,
    send_ref: str,
    prompt_digest: str,
    prompt_chars: int,
    epoch_id: str,
    source_refs: tuple[str, ...] = (),
    provider_effect_id: str = "",
    model_tool_contract_hash: str = "",
    runtime_tool_contract_hash: str = "",
    sections: tuple[PromptSurfaceSection, ...] = (),
) -> PromptSurfaceRecord:
    norm_phase = canonical_surface_phase(phase)
    norm_send_ref = canonical_surface_send_ref(send_ref)
    norm_prompt_digest = canonical_surface_prompt_digest(prompt_digest)
    surface = prompt_surface_id(phase=norm_phase, send_ref=norm_send_ref, prompt_digest=norm_prompt_digest)
    return PromptSurfaceRecord(
        surface_id=surface,
        phase=norm_phase,
        prompt_digest=norm_prompt_digest,
        prompt_chars=max(0, int(prompt_chars or 0)),
        epoch_id=str(epoch_id or "").strip()[:80],
        send_ref=norm_send_ref,
        source_refs=tuple(str(r).strip()[:120] for r in source_refs or () if str(r).strip())[:16],
        provider_effect_id=str(provider_effect_id or "").strip()[:80],
        model_tool_contract_hash=str(model_tool_contract_hash or "").strip()[:80],
        runtime_tool_contract_hash=str(runtime_tool_contract_hash or "").strip()[:80],
        sections=tuple(sections or ()),
        schema_version=PROMPT_SURFACE_SCHEMA_VERSION,
    )


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return bool(isinstance(value, str) and pattern.fullmatch(value))


def _is_sha256(value: object) -> bool:
    return _matches(_SHA256_RE, value)


def _is_surface_id(value: object) -> bool:
    return _matches(_SURFACE_RE, value)


def _is_phase(value: object) -> bool:
    return bool(isinstance(value, str) and 1 <= len(value) <= 40 and _IDENTIFIER_RE.fullmatch(value))


def _is_send_ref(value: object) -> bool:
    return bool(isinstance(value, str) and _SEND_REF_RE.fullmatch(value))


def validate_prompt_surface_payload(payload: Mapping[str, object]) -> bool:
    if not isinstance(payload, Mapping):
        return False
    # exact forbidden keys are rejected
    for key in payload:
        if str(key) in _FORBIDDEN_PAYLOAD_KEYS:
            return False
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != PROMPT_SURFACE_SCHEMA_VERSION:
        return False
    surface_id = payload.get("surface_id")
    send_ref = payload.get("send_ref")
    phase = payload.get("phase")
    prompt_digest = payload.get("prompt_digest")
    prompt_chars = payload.get("prompt_chars")
    epoch_id = payload.get("epoch_id")
    if not _is_surface_id(surface_id):
        return False
    # phase must be canonical: 1..40 chars, no extra whitespace or non-identifier characters
    if not _is_phase(phase) or phase != canonical_surface_phase(phase):
        return False
    # send_ref must be canonical: 1..80 chars, strict identifier charset, no whitespace or newlines
    if (
        not _is_send_ref(send_ref)
        or send_ref != canonical_surface_send_ref(send_ref)
    ):
        return False
    # prompt_digest must be canonical: strict sha256 pattern, no whitespace
    if (
        not isinstance(prompt_digest, str)
        or not _is_sha256(prompt_digest)
        or prompt_digest != canonical_surface_prompt_digest(prompt_digest)
    ):
        return False
    if surface_id != prompt_surface_id(phase=phase, send_ref=send_ref, prompt_digest=prompt_digest):
        return False
    if type(prompt_chars) is not int or prompt_chars < 0 or prompt_chars > 10_000_000:
        return False
    # epoch_id is required and must strictly match ctx_epoch pattern
    if not _matches(_EPOCH_RE, epoch_id):
        return False
    # optional hashes, if present must be sha256
    for key in ("model_tool_contract_hash", "runtime_tool_contract_hash"):
        val = payload.get(key)
        if val not in (None, "") and not _is_sha256(val):
            return False
    sections = payload.get("sections")
    if sections is not None:
        if not isinstance(sections, (list, tuple)):
            return False
        if len(sections) > 80:
            return False
        for item in sections:
            if not isinstance(item, Mapping):
                return False
            for k in item:
                if str(k) in _FORBIDDEN_PAYLOAD_KEYS:
                    return False
            name = item.get("name")
            digest = item.get("digest")
            chars = item.get("chars")
            if not isinstance(name, str) or not name.strip() or not _is_sha256(digest):
                return False
            if type(chars) is not int or chars < 0:
                return False
            epoch = item.get("epoch_id")
            if epoch not in (None, "") and not _matches(_EPOCH_RE, epoch):
                return False
    return True
