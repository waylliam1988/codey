from __future__ import annotations

import json
from typing import Protocol, runtime_checkable


class _ArtifactStore(Protocol):
    def write_json_artifact(
        self, *, session_id: str, run_id: str, payload: object, schema_version: int,
    ) -> object: ...


@runtime_checkable
class _ArtifactReference(Protocol):
    def to_payload(self) -> object: ...


def externalize_value(
    value: object,
    *,
    store: _ArtifactStore | None,
    session_id: str,
    run_id: str,
    max_inline_bytes: int,
) -> object:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) <= max_inline_bytes:
        return value
    if store is None:
        raise ValueError("large CodeMode output requires an artifact store")
    try:
        ref = store.write_json_artifact(session_id=session_id, run_id=run_id, payload=value, schema_version=1)
    except OSError as exc:
        raise ValueError(f"CodeMode output could not be stored: {exc}") from exc
    if not isinstance(ref, _ArtifactReference):
        raise ValueError("artifact store returned an invalid reference")
    to_payload = ref.to_payload
    if not callable(to_payload):
        raise ValueError("artifact store returned an invalid reference")
    return {"artifact": to_payload()}
