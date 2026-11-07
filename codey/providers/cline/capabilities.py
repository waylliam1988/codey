"""Exact capability contracts; aliases are explicit, never slug guesses."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from codey.runtime.core.reasoning import normalize_efforts

# Reviewed against Cline's free feed and the public OpenRouter catalog, 2026-11-05.
ALIASES = {"cline-free/solar-mini4": "upstage/solar-mini4",
           "cline-free/nemotron-3.5-super-vl": "nvidia/nemotron-3.5-super-vl",
           "nvidia/nemotron-3.5-lightning-free": "nvidia/nemotron-3.5-lightning",
           "reflection/beam-free": "reflection/beam-1.0-501b-a23b"}


@dataclass(frozen=True)
class Capability:
    context: int
    output: int
    tools: bool
    efforts: tuple[str, ...] = ()
    toggle: bool = False
    input_limit: int | None = None
    images: bool = False

    @property
    def revision(self) -> str:
        values = asdict(self)
        if not self.images:
            values.pop("images")
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:24]


def parse_capability(value: dict[str, Any]) -> Capability | None:
    context = value.get("context_length")
    top = value.get("top_provider") or {}
    output = top.get("max_completion_tokens") if isinstance(top, dict) else None
    if type(context) is not int or type(output) is not int or context <= 1024 or output <= 0:
        return None
    parameters = value.get("supported_parameters") or []
    if not isinstance(parameters, list):
        return None
    reasoning = value.get("reasoning") or {}
    if not isinstance(reasoning, dict):
        return None
    declared = reasoning.get("supported_efforts") or []
    efforts = normalize_efforts(declared) if isinstance(declared, list) and "reasoning" in parameters else ()
    # Only an explicit non-mandatory reasoning declaration enables this switch.
    toggle = "reasoning" in parameters and reasoning.get("mandatory") is False and not efforts
    architecture = value.get("architecture") or {}
    modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    images = isinstance(modalities, list) and "image" in modalities
    return Capability(context, min(output, context - 1), "tools" in parameters, efforts, toggle, images=images)
