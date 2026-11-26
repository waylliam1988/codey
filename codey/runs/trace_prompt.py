"""PromptTraceRecorder: domain recording and deduplication with shared flush scheduling."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from codey.runs.text_clip import clip_text as _clip
from codey.runs.trace_models import (
    PromptSectionTrace,
    PromptSurfaceTrace,
    RunTraceManifest,
)
from codey.runs.trace_schema import MAX_PROMPT_SECTIONS, MAX_PROMPT_SURFACES, MAX_REFS
from codey.runs.trace_values import _bounded_refs, _identifier, _nonnegative_int, _trace_list_items
from codey.runtime.observe.prompt_envelope import is_model_boundary_freshness
from codey.utils.refs import digest_text
from codey.workspace.context_epoch import admission_from_rendered_source


class PromptTraceRecorder:
    def __init__(self, manifest: RunTraceManifest, *, checkpoint: Callable[[], None], flush: Callable[[], None]) -> None:
        self.manifest = manifest
        self.checkpoint = checkpoint
        self.flush = flush
        self._prompt_keys: set[
            tuple[str, str, str, str, tuple[str, ...], bool, str]
        ] = set()
        self._local_context_keys: set[tuple[str, str, str]] = set()
        self._prompt_surface_keys: set[str] = set()

    def _append_prompt_section(
        self,
        name: str,
        text: object,
        *,
        purpose: str = "",
        model_visible: bool = True,
        budget: int = 0,
        truncated: bool = False,
        freshness: str = "",
        source_refs: Iterable[object] = (),
        epoch_id: str = "",
        admission_reason: str = "",
        capability_id: str = "",
        scope: str = "",
    ) -> bool:
        rendered = str(text or "")
        if not rendered:
            return False
        refs = _bounded_refs(source_refs)
        if not refs and model_visible:
            fallback = _identifier(name, 80) or "prompt_section"
            refs = (f"prompt_section:{fallback}",)
        item = PromptSectionTrace(
            name=name,
            digest=digest_text(rendered),
            chars=len(rendered),
            purpose=(purpose or ""),
            model_visible=model_visible,
            budget=_nonnegative_int(budget),
            truncated=truncated,
            freshness=freshness,
            source_refs=refs,
            epoch_id=_identifier(epoch_id, 80),
            admission_reason=_identifier(admission_reason, 80),
            capability_id=_identifier(capability_id, 80),
            scope=_clip(scope, 160),
        )
        key = (
            item.name,
            item.digest,
            item.purpose,
            item.freshness,
            refs,
            item.model_visible,
            item.epoch_id,
        )
        if key in self._prompt_keys:
            return False
        self._prompt_keys.add(key)
        self.manifest.prompt_sections.append(item)
        if len(self.manifest.prompt_sections) > MAX_PROMPT_SECTIONS:
            del self.manifest.prompt_sections[:-MAX_PROMPT_SECTIONS]
            self.manifest.warnings.append("prompt_sections_truncated")
        return True

    def record_prompt_section(
        self,
        name: str,
        text: object,
        *,
        purpose: str = "",
        model_visible: bool = True,
        budget: int = 0,
        truncated: bool = False,
        freshness: str = "",
        source_refs: Iterable[object] = (),
        epoch_id: str = "",
        admission_reason: str = "",
        capability_id: str = "",
    ) -> None:
        model_boundary = is_model_boundary_freshness(freshness)
        added = self._append_prompt_section(
            name,
            text,
            purpose=purpose,
            model_visible=model_visible,
            budget=budget,
            truncated=truncated,
            freshness=freshness,
            source_refs=source_refs,
            epoch_id=epoch_id,
            admission_reason=admission_reason,
            capability_id=capability_id,
        )
        if model_boundary:
            self.flush()
        elif added:
            self.checkpoint()

    def _append_prompt_surface(self, payload: object) -> bool:
        try:
            from codey.runtime.observe.prompt_surface import validate_prompt_surface_payload
        except Exception:
            return False
        if not isinstance(payload, Mapping):
            return False
        if not validate_prompt_surface_payload(payload):
            return False
        surface_id = _clip(payload.get("surface_id"), 120)
        send_ref = _clip(payload.get("send_ref"), 80)
        if surface_id in self._prompt_surface_keys:
            return False
        # bounded section payloads already validated
        sections: tuple[dict[str, object], ...] = ()
        raw_sections = payload.get("sections")
        if isinstance(raw_sections, (list, tuple)):
            cleaned: list[dict[str, object]] = []
            for item in raw_sections[:8]:
                if not isinstance(item, Mapping):
                    continue
                cleaned.append({
                    "name": _identifier(item.get("name"), 80),
                    "digest": _clip(item.get("digest"), 80),
                    "chars": _nonnegative_int(item.get("chars")),
                    "source_refs": list(
                        _bounded_refs(_trace_list_items(item.get("source_refs")))
                    ),
                    "model_visible": bool(item.get("model_visible", True)),
                    "freshness": _identifier(item.get("freshness"), 80),
                    "epoch_id": _identifier(item.get("epoch_id"), 80),
                    "capability_id": _identifier(item.get("capability_id"), 80),
                })
            sections = tuple(cleaned)
        schema_version = payload["schema_version"]
        if not isinstance(schema_version, int):
            return False
        item = PromptSurfaceTrace(
            surface_id=surface_id,
            phase=_identifier(payload.get("phase"), 40),
            prompt_digest=_clip(payload.get("prompt_digest"), 80),
            prompt_chars=_nonnegative_int(payload.get("prompt_chars")),
            epoch_id=_identifier(payload.get("epoch_id"), 80),
            send_ref=send_ref,
            source_refs=_bounded_refs(_trace_list_items(payload.get("source_refs"))),
            provider_effect_id=_clip(payload.get("provider_effect_id"), 80),
            model_tool_contract_hash=_clip(payload.get("model_tool_contract_hash"), 80),
            runtime_tool_contract_hash=_clip(payload.get("runtime_tool_contract_hash"), 80),
            sections=sections,
            schema_version=schema_version,
        )
        self._prompt_surface_keys.add(surface_id)
        self.manifest.prompt_surfaces.append(item)
        if len(self.manifest.prompt_surfaces) > MAX_PROMPT_SURFACES:
            del self.manifest.prompt_surfaces[:-MAX_PROMPT_SURFACES]
            self.manifest.warnings.append("prompt_surfaces_truncated")
        return True

    def record_prompt_surface(self, payload: Mapping[str, object]) -> None:
        if self._append_prompt_surface(payload):
            self.flush()

    def record_provider_prompt_boundary(
        self,
        section_args: Mapping[str, object],
        surface_payload: Mapping[str, object] | None = None,
    ) -> None:
        if isinstance(section_args, Mapping):
            self._append_prompt_section(
                name=str(section_args.get("name") or ""),
                text=section_args.get("text"),
                purpose=str(section_args.get("purpose") or ""),
                model_visible=bool(section_args.get("model_visible", True)),
                budget=_nonnegative_int(section_args.get("budget")),
                truncated=bool(section_args.get("truncated", False)),
                freshness=str(section_args.get("freshness") or ""),
                source_refs=_trace_list_items(section_args.get("source_refs")),
                epoch_id=str(section_args.get("epoch_id") or ""),
                admission_reason=str(section_args.get("admission_reason") or ""),
                capability_id=str(section_args.get("capability_id") or ""),
            )
        if surface_payload is not None:
            self._append_prompt_surface(surface_payload)
        self.flush()

    def record_context_sources(
        self,
        sources: Iterable[Any],
        *,
        epoch_id: str = "",
        admission_reason: str = "",
    ) -> None:
        """Record rendered context sources as bounded prompt-section rows.

        Rows are projected through the shared ContextEpoch admission
        projection, so trace rows and snapshots share one ref/digest
        vocabulary. When epoch_id is supplied (the outbound prompt's
        content-addressed epoch), each row is bound to that provider turn;
        the per-source admission_reason wins over the caller's fallback.
        """
        normalized_epoch = _identifier(epoch_id, 80)
        changed = False
        for source in sources:
            admission = admission_from_rendered_source(
                source,
                admission_reason=admission_reason,
            )
            if admission is None:
                continue
            item = PromptSectionTrace(
                name=admission.source_key,
                digest=admission.digest,
                chars=admission.chars,
                purpose=str(getattr(source, "why_included", "") or ""),
                model_visible=True,
                budget=admission.budget,
                truncated=admission.truncated,
                freshness=str(getattr(source, "freshness", "") or ""),
                source_refs=(admission.source_ref,),
                epoch_id=normalized_epoch,
                admission_reason=admission.admission_reason,
                capability_id=admission.capability_id,
                scope=admission.scope,
                expires_at=admission.expires_at,
            )
            key = (
                item.name,
                item.digest,
                item.purpose,
                item.freshness,
                item.source_refs,
                item.model_visible,
                item.epoch_id,
            )
            if key in self._prompt_keys:
                continue
            self._prompt_keys.add(key)
            self.manifest.prompt_sections.append(item)
            changed = True
        if changed:
            if len(self.manifest.prompt_sections) > MAX_PROMPT_SECTIONS:
                del self.manifest.prompt_sections[:-MAX_PROMPT_SECTIONS]
                self.manifest.warnings.append("prompt_sections_truncated")
            self.checkpoint()

    def record_local_context_refs(self, refs: Iterable[object]) -> None:
        changed = False
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            item_id = _clip(ref.get("id"), 120)
            scope = _identifier(ref.get("scope"), 40)
            kind = _identifier(ref.get("kind"), 80)
            if not item_id:
                continue
            key = (item_id, scope, kind)
            if key in self._local_context_keys:
                continue
            self._local_context_keys.add(key)
            payload: dict[str, object] = {"id": item_id}
            if scope:
                payload["scope"] = scope
            if kind:
                payload["kind"] = kind
            source = _identifier(ref.get("source"), 80)
            if source:
                payload["source"] = source
            self.manifest.local_context_refs.append(payload)
            changed = True
        if changed:
            if len(self.manifest.local_context_refs) > MAX_REFS:
                del self.manifest.local_context_refs[:-MAX_REFS]
                self.manifest.warnings.append("local_context_refs_truncated")
            self.checkpoint()
