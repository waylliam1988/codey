from __future__ import annotations

import json
from typing import Any


def externalize_value(
    value: object,
    *,
    store: Any,
    session_id: str,
    run_id: str,
    max_inline_bytes: int,
) -> object:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) <= max_inline_bytes:
        return value
    if store is None:
        raise ValueError("large CodeMode output requires an artifact store")
    ref = store.write_json_artifact(
        session_id=session_id,
        run_id=run_id,
        payload=value,
        schema_version=1,
    )
    to_payload = getattr(ref, "to_payload", None)
    if not callable(to_payload):
        raise ValueError("artifact store returned an invalid reference")
    return {"artifact": to_payload()}
