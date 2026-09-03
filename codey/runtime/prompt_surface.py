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

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EPOCH_RE = re.compile(r"^ctx_epoch:[0-9a-f]{16}$")
_SURFACE_RE = re.compile(r"^prompt_surface:[0-9a-f]{16}$")


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
    norm_phase = str(phase or "").strip()[:40]
    norm_send_ref = str(send_ref or "").strip()[:80]
    norm_prompt_digest = str(prompt_digest or "").strip()[:80]
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


def _is_sha256(value: object) -> bool:
    return bool(_SHA256_RE.match(str(value or "").strip()))


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
    surface_id = str(payload.get("surface_id") or "").strip()
    send_ref = str(payload.get("send_ref") or "").strip()
    phase = str(payload.get("phase") or "").strip()
    prompt_digest = str(payload.get("prompt_digest") or "").strip()
    prompt_chars = payload.get("prompt_chars")
    epoch_id = str(payload.get("epoch_id") or "").strip()
    if not _SURFACE_RE.match(surface_id):
        return False
    if not send_ref:
        return False
    if not phase:
        return False
    if not _is_sha256(prompt_digest):
        return False
    if surface_id != prompt_surface_id(phase=phase, send_ref=send_ref, prompt_digest=prompt_digest):
        return False
    if type(prompt_chars) is not int or prompt_chars < 0 or prompt_chars > 10_000_000:
        return False
    if epoch_id and not _EPOCH_RE.match(epoch_id):
        return False
    # optional hashes, if present must be sha256
    for key in ("model_tool_contract_hash", "runtime_tool_contract_hash"):
        val = payload.get(key)
        if val not in (None, "") and str(val).strip() and not _is_sha256(val):
            return False
    provider_effect_id = payload.get("provider_effect_id")
    if provider_effect_id not in (None, "") and str(provider_effect_id).strip():
        # provider_effect_id is opaque but bounded; no strict format, just non-empty string allowed
        if not str(provider_effect_id).strip():
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
            name = str(item.get("name") or "").strip()
            digest = str(item.get("digest") or "").strip()
            chars = item.get("chars")
            if not name or not _is_sha256(digest):
                return False
            if type(chars) is not int or chars < 0:
                return False
            epoch = item.get("epoch_id")
            if epoch not in (None, "") and not _EPOCH_RE.match(str(epoch)):
                return False
    return True
