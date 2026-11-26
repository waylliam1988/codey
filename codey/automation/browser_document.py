"""Compare observed document identities independently of mutable URL state."""
from __future__ import annotations

from codey.types.json_value import decode_json_object


def normalize_element_ref(ref: str) -> str:
    """Accept a ref copied from the human-readable snapshot rows."""
    ref = ref.strip()
    return ref[1:-1] if ref.startswith("[") and ref.endswith("]") else ref


def document_identity(value: object) -> tuple[str, str, str] | None:
    if not isinstance(value, dict):
        return None
    document = decode_json_object(value)
    loader, target, frame = document.get('loader_id'), document.get('target_id'), document.get('frame_id')
    if not isinstance(loader, str) or not loader or not isinstance(target, str) or not target or not isinstance(frame, str) or not frame:
        return None
    return target, frame, loader


def same_document(left: object, right: object) -> bool:
    before, after = document_identity(left), document_identity(right)
    return before is not None and after is not None and before == after


def document_changed(left: object, right: object) -> bool:
    """Missing identity cannot prove that an unsettled action changed documents."""
    before = document_identity(left)
    if before is None:
        return False
    after = document_identity(right)
    return after is not None and before != after
