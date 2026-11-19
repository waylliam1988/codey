"""Replay the saved invalid completion after real browser/edit receipts, then use the local model.

The preparation replies are explicitly scripted. They use production
tools, including the injected stale-edit rejection and fresh-read recovery.
After the saved bad done, model requests and responses are not rewritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from codey.providers.base import ProviderToolDefinition
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tools.browser_production_live_gate import run_case, select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    write_json,
)

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/hybrid_knowledge_failure_20261118.json"


def preparation_replies() -> list[JsonObject]:
    # The production entry has already navigated to the declared URL. Return
    # source evidence before using its frozen destination-only copy input.
    acquisition = (
        "let s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_fill({ref:s.elements.find(e=>e.name==='Locality').ref,value:'福州'}));"
        "s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_click({ref:s.elements.find(e=>e.name==='Load record').ref}));"
        "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));"
        "return unwrap(await tools.browser_read({}));"
    )
    fixture = parse_json_object(FIXTURE.read_text(encoding="utf-8"))
    return [
        {"tool": "codemode", "args": {"code": acquisition, "inputs": {}}},
        {"tool": "codemode", "args": {"path": "result.json"}},
        {"tool": "codemode", "args": {"path": "result.json"}},
        parse_json_object(require_json_string(fixture["raw_reply"], "saved reply")),
    ]


def run_recovery(directory: Path, target: Target) -> JsonObject:
    replies = preparation_replies()

    class PrefixProvider(ExperimentProvider):
        def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                      *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                      tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                      tool_call_contract: str = "", tool_call_observations: str = "",
                      tool_call_history_groups: int = 0, output_token_limit: int = 0,
                      require_tool_call: bool = False) -> dict[str, object]:
            if self.exchange < len(replies):
                reply = replies[self.exchange]
                self.exchange += 1
                body: dict[str, object] = {"choices": [{"message": {
                    "role": "assistant", "content": json.dumps(reply, ensure_ascii=False), "tool_calls": []},
                    "finish_reason": "stop"}]}
                append_json(self.directory / "provider.jsonl", {"type": "scripted_recovery_prefix",
                            "exchange": self.exchange, "payload": body})
                return body
            return super()._generate(messages, tools, timeout=timeout, checkpoints=checkpoints,
                tool_call_json=tool_call_json, tool_call_schema=tool_call_schema, tool_call_contract=tool_call_contract,
                tool_call_observations=tool_call_observations, tool_call_history_groups=tool_call_history_groups,
                output_token_limit=output_token_limit, require_tool_call=require_tool_call)

    with patch("tools.browser_production_live_gate.ExperimentProvider", PrefixProvider):
        result = run_case(directory, "hybrid", target, stale_edit=True, codemode_only=True)
    receipts = tool_receipts(directory / "state")
    calls = Counter(receipt.result.call.name for receipt in receipts)
    edits = [index for index, receipt in enumerate(receipts) if receipt.result.call.name == "edit"]
    continuation = receipts[edits[-1] + 1:] if edits else []
    repeated = [row.identity for row in continuation
                if row.result.call.name.startswith("browser_") or row.result.call.name in {"read_file", "edit"}]
    wire = [parse_json_object(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    requests = [row for row in wire if row.get("type") == "wire_attempt" and row.get("phase") == "request"]
    feedback_seen = any("done.knowledge_refs rejected:" in json.dumps(row["payload"], ensure_ascii=False)
                        and "omit optional knowledge_refs" in json.dumps(row["payload"], ensure_ascii=False)
                        for row in requests)
    expected_counts = {"browser_navigate": 1, "browser_fill": 1, "browser_click": 1, "browser_read": 1,
                       "read_file": 2, "edit": 2}
    no_replay = not repeated and all(calls[name] == count for name, count in expected_counts.items())
    proposals = [parse_json_object(line) for line in (directory / "proposals.jsonl").read_text(encoding="utf-8").splitlines()]
    final = parse_json_object(require_json_string(proposals[-1]["normalized_reply"], "final proposal")) if proposals else {}
    omitted = "knowledge_refs" not in decode_json_object(final.get("args", {}))
    summary: JsonObject = {
        "passed": result.get("passed") is True and feedback_seen and no_replay and omitted,
        "stop_reason": str(result.get("stop_reason")), "scripted_generations": len(replies),
        "saved_failure_fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "continuation_after_receipt_index": edits[-1] if edits else None,
        "first_tool_after_preparation": continuation[0].result.call.name if continuation else None,
        "tool_calls": dict(calls), "repeated_actions": list(repeated), "feedback_in_actual_request": feedback_seen,
        "final_optional_reference_omitted": omitted,
        "model": target.model, **provider_metrics(directory / "provider.jsonl"),
        "scope": "scripted_browser_and_stale_edit_recovery_then_saved_bad_done_then_live_model",
    }
    write_json(directory / "continuation-summary.json", summary)
    return summary


class Options(argparse.Namespace):
    run_dir: Path
    timeout: float
    kobold_base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Options())
    target = select_target(args.timeout, args.kobold_base_url)
    before = production_source_digest()
    result = run_recovery(args.run_dir.resolve(), target)
    result.update(source_before=before, source_after=production_source_digest())
    write_json(args.run_dir / "summary.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
