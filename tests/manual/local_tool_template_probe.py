"""Read KoboldCpp's rendered tool history without generating model output."""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any


def read_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("metadata response exceeded limit")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("metadata response is not an object")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--output", type=Path, default=Path(".e2e-artifacts/qwen-thinking-20261031/template-probe.json"))
    args = parser.parse_args()
    root = args.base_url.rstrip("/")
    version = read_json(root + "/api/extra/version")
    model = read_json(root + "/v1/models")["data"][0]["id"]
    payload = {"model": model, "stream": False, "messages": [
        {"role": "user", "content": "Plan a code review."},
        {"role": "assistant", "content": "\n\n", "reasoning_content": "Register questions.", "tool_calls": [
            {"id": "probe-1", "type": "function", "function": {
                "name": "research_plan", "arguments": '{"questions":["Which files need review?"]}'}}]},
        {"role": "tool", "tool_call_id": "probe-1", "content": "TOOL_RECEIPT_SENTINEL: The plan is already recorded."},
        {"role": "user", "content": "CORRECTION_SENTINEL: Continue from the recorded plan. Do not repeat it."},
    ], "tools": [{"type": "function", "function": {"name": "research_plan", "description": "Register questions",
        "parameters": {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
                       "required": ["questions"]}}}]}
    rendered = read_json(root + "/api/extra/tokencount", payload)
    prompt = rendered.get("prompt", "")
    checks = {"tool_result_present": "TOOL_RECEIPT_SENTINEL" in prompt,
              "correction_present": "CORRECTION_SENTINEL" in prompt,
              "reasoning_present": "Register questions." in prompt}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"version": version, "model": model, "request": payload,
                                      "rendered": rendered, "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
    if not all(checks.values()):
        raise SystemExit(f"Template lost history: {checks}")
    print(json.dumps({"checks": checks, "tokens": rendered.get("value"), "output": str(args.output)}))


if __name__ == "__main__":
    main()
