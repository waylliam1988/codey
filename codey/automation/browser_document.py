"""Compare observed document identities independently of mutable URL state."""
from __future__ import annotations

from codey.types.json_value import decode_json_object


def normalize_element_ref(ref: str) -> str:
    """Accept a ref copied from the human-readable snapshot rows."""
    ref = ref.strip()
    return ref[1:-1] if ref.startswith("[") and ref.endswith("]") else ref


def _identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    document = decode_json_object(value)
    loader, target = document.get('loader_id'), document.get('target_id', '')
    if not isinstance(loader, str) or not loader or not isinstance(target, str):
        return None
    return target, loader


def same_document(left: object, right: object) -> bool:
    before, after = _identity(left), _identity(right)
    if before is None or after is None:
        return False
    # Historical receipts lack target_id. Preserve their loader binding rather
    # than treating the newly recorded target field as a navigation.
    return before[1] == after[1] and (not before[0] or not after[0] or before[0] == after[0])


def document_changed(left: object, right: object) -> bool:
    """Missing identity cannot prove that an unsettled action changed documents."""
    return _identity(left) is not None and _identity(right) is not None and not same_document(left, right)
