"""A prompt example cannot establish that a fixed copy was actually offered."""
from __future__ import annotations

import json
from pathlib import Path

from tools.unified_natural_browser_probe import fixed_copy_exchanges


def test_actual_argument_schema_owns_fixed_copy_diagnostics(tmp_path: Path) -> None:
    def request(exchange: int, name: str, required: str) -> dict[str, object]:
        return {"exchange": exchange, "phase": "request", "payload": {
            "messages": [{"role": "system", "content": '{"tool":"codemode","args":{"path":"result.json"}}'}],
            "response_format": {"json_schema": {"schema": {"oneOf": [{"allOf": [
                {"properties": {"tool": {"enum": [name]}}},
                {"properties": {"args": {"required": [required], "properties": {required: {"type": "string"}}}}}
            ]}]}}}}}
    rows = [request(1, "codemode", "code"), request(2, "codemode", "path"), request(3, "edit", "path")]
    (tmp_path / "provider.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert fixed_copy_exchanges(tmp_path) == [2, 3]
