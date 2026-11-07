"""Operator-authenticated file import and model input validation."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.runtime.core.api_selection import ApiRunSelection

if TYPE_CHECKING:
    from codey.app.context import AppContext

from codey.storage.input_attachments import InputAttachmentStore
from codey.task.attachments import IMAGE_UNSUPPORTED, public_attachment, validate_attachments


def _store(ctx: AppContext, session_id: object) -> InputAttachmentStore:
    sessions = ctx.load_ui_state().get("sessions", [])
    if not isinstance(session_id, str) or not any(s.get("id") == session_id for s in sessions):
        raise ValueError("Chat not found. Save the chat before attaching files.")
    if ctx.state_home is None:
        raise ValueError("Attachment storage is unavailable.")
    return InputAttachmentStore(ctx.state_home, session_id)


def upload_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    try:
        item = _store(ctx, body.get("session_id")).put(body.get("name"), body.get("data"))
        return 201, {"ok": True, "attachment": item}
    except (ValueError, OSError) as exc:
        return 400, {"ok": False, "error": str(exc)}


def read_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    try:
        item = _read(ctx, str(body.get("session_id") or ""), body.get("id"))
        return 200, {"ok": True, "attachment": public_attachment(item), **({"data_url": item["data_url"]} if "data_url" in item else {})}
    except (ValueError, OSError) as exc:
        return 404, {"ok": False, "error": str(exc)}


def image_support(ctx: AppContext, session_id: str, provider: str, model: str) -> bool:
    active = ctx.active_run_for(session_id=session_id)
    if active is not None:
        selected = ctx.run_registry.api_selection_for(active.run_id)
        return bool(selected is not None and selected.supports_images)
    from codey.providers.api_connections import cached_model_supports_images

    return cached_model_supports_images(provider, model)


def capabilities_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    try:
        session_id = str(body.get("session_id") or "")
        _store(ctx, session_id)
        selection: object = body.get("model_selection") or {}
        if not isinstance(selection, dict):
            raise ValueError("Invalid model selection.")
        images = image_support(ctx, session_id, str(body.get("provider") or ""), str(selection.get("model") or ""))
        return 200, {"ok": True, "images": images}
    except (ValueError, OSError) as exc:
        return 400, {"ok": False, "error": str(exc)}


def resolve_input_attachments(ctx: AppContext, session_id: str, refs: object, *, selection: ApiRunSelection | None = None, steer: bool = False) -> list[dict[str, object]]:
    if not isinstance(refs, list) or len(refs) > 4 or any(not isinstance(ref, dict) for ref in refs):
        raise ValueError("Invalid attachment references.")
    items = validate_attachments([_read(ctx, session_id, ref.get("id")) for ref in refs])
    supported = bool(selection is not None and selection.supports_images)
    if steer:
        active = ctx.active_run_for(session_id=session_id)
        if active is not None:
            current = ctx.run_registry.api_selection_for(active.run_id)
            supported = bool(current is not None and current.supports_images)
    if not supported and any(str(item["media_type"]).startswith("image/") for item in items):
        raise ValueError(IMAGE_UNSUPPORTED)
    return items


def _read(ctx: AppContext, session_id: str, identity: object) -> dict[str, object]:
    store = _store(ctx, session_id)
    try:
        return store.read(identity)
    except FileNotFoundError:
        # A fork inherits immutable attachment content through its canonical
        # ancestry, without granting access to unrelated chats' upload stores.
        if ctx.durable.has_conversation(session_id):
            from codey.durable.model import thaw_json

            for entry in ctx.durable.context(session_id):
                if entry.kind == "user_message":
                    payload = thaw_json(entry.payload)
                    if not isinstance(payload, dict):
                        raise ValueError("invalid durable user message") from None
                    attachments = payload.get("attachments", [])
                    if not isinstance(attachments, list) or any(not isinstance(row, dict) for row in attachments):
                        raise ValueError("invalid durable attachments") from None
                    for item in attachments:
                        if item.get("id") == identity:
                            return validate_attachments([item])[0]
        raise


def queued_attachments(ctx: AppContext, request: dict[str, object], selection: ApiRunSelection | None) -> tuple[ApiRunSelection | None, dict[str, object]]:
    if request.get("input_mode") == "steer":
        active = ctx.active_run_for(session_id=str(request["session_id"]))
        if active is not None:
            request["provider"] = active.provider_id
            selection = ctx.run_registry.api_selection_for(active.run_id)
    items = resolve_input_attachments(ctx, str(request["session_id"]), request["attachments"], selection=selection, steer=request.get("input_mode") == "steer")
    request["attachments"] = [public_attachment(item) for item in items]
    return selection, {"attachments": items}


def prepare_run_attachments(ctx: AppContext | None, session_id: str, refs: object, kwargs: dict[str, object]) -> tuple[int, dict[str, object]] | None:
    try:
        if refs == []:
            return None
        if ctx is None:
            raise ValueError("Attachment storage is unavailable.")
        selection = kwargs.get("model_selection")
        if selection is not None and not isinstance(selection, ApiRunSelection):
            raise ValueError("Invalid model selection.")
        items = resolve_input_attachments(ctx, session_id, refs, selection=selection)
        kwargs["message_metadata"] = {"attachments": items}
        return None
    except (ValueError, OSError) as exc:
        return 400, {"error": str(exc), "code": "attachment_invalid"}


def prepare_queued_attachments(ctx: AppContext, request: dict[str, object], selection: ApiRunSelection | None) -> tuple[ApiRunSelection | None, dict[str, object] | None, tuple[int, dict[str, object]] | None]:
    if "attachments" not in request or request["attachments"] == []:
        return selection, None, None
    try:
        selection, metadata = queued_attachments(ctx, request, selection)
        return selection, metadata, None
    except (ValueError, OSError) as exc:
        return selection, None, (400, {"ok": False, "error": str(exc), "code": "attachment_invalid"})
