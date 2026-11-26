from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from codey.runtime.core.models import (
    canonical_json_mapping,
    json_safe_projection,
    model_text_with_audit_markers,
    normalized_managed_output,
)


@dataclass(frozen=True)
class ToolOutcome:
    model_text: str
    ok: bool
    canonical: Mapping[str, object] = field(default_factory=dict)
    presentation: Mapping[str, object] = field(default_factory=dict)
    audit: Mapping[str, object] = field(default_factory=dict)
    error_code: str = ""
    exit_code: int | None = None
    changed: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        model_text = (self.model_text or "")
        error_code = (self.error_code or ("error" if not self.ok else ""))
        presentation = json_safe_projection(self.presentation, label="presentation")
        presentation.setdefault("status", "ok" if self.ok else "error")
        presentation.setdefault("result", _first_model_line(model_text, 200))
        audit = json_safe_projection(self.audit, label="audit")
        if error_code:
            audit["error_code"] = error_code
        # Strict exit: only real ints are projected. Bool/str/float exits
        # are omitted instead of being coerced (bool False must not become 0).
        strict_code = next((value for value in (self.exit_code, audit.get("exit_code")) if type(value) is int), None)
        object.__setattr__(self, "exit_code", strict_code)
        if strict_code is not None:
            audit["exit_code"] = strict_code
        else:
            audit.pop("exit_code", None)
        if self.changed:
            audit["changed"] = True
        if self.truncated:
            audit["truncated"] = True
        canonical = canonical_json_mapping(self.canonical)
        model_text = model_text_with_audit_markers(
            model_text,
            truncated=self.truncated,
            audit=audit,
        )
        object.__setattr__(self, "model_text", model_text)
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "canonical", canonical)
        object.__setattr__(self, "presentation", presentation)
        object.__setattr__(self, "audit", audit)

    @classmethod
    def error(
        cls,
        message: str,
        *,
        error_code: str = "error",
        audit: Mapping[str, object] | None = None,
    ) -> ToolOutcome:
        text = message if message.startswith("ERROR:") else f"ERROR: {message}"
        audit_payload = dict(audit or {})
        audit_payload["error_code"] = error_code
        return cls(
            text,
            False,
            presentation={"status": "error", "result": _first_model_line(text, 200)},
            audit=audit_payload,
            error_code=error_code,
        )

    def first_model_line(self, limit: int) -> str:
        """Return a display-safe first model-visible line, including empty text."""
        return _first_model_line(self.model_text, limit)

    def presentation_result(self, limit: int) -> str:
        value = self.presentation.get("result") if isinstance(self.presentation, Mapping) else ""
        text = str(value or "") or self.first_model_line(limit)
        return text[:limit]

    def presentation_status(self) -> str:
        value = self.presentation.get("status") if isinstance(self.presentation, Mapping) else ""
        return str(value or "") or ("ok" if self.ok else "error")

    def managed_output(self) -> Mapping[str, object]:
        value = self.audit.get("managed_output") if isinstance(self.audit, Mapping) else None
        return normalized_managed_output(value)


def _first_model_line(text: object, limit: int) -> str:
    return next(iter(str(text or "").splitlines()), "")[:limit]
