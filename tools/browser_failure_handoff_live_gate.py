"""Replay one saved failed reply, then let the local model use production handoffs.

Only the saved first reply is scripted. Tool contracts, execution, receipts,
initial prerequisites and all subsequent model generations stay in production.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import TurnSnapshot, normalize_turn
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import ProviderToolDefinition
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tools.browser_leaf_exposure_ab import response_actions
from tools.browser_production_live_gate import run_case, select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
CASES = ("basic", "download", "wrong_url")


def saved_prefix(case: str) -> tuple[str, JsonObject]:
    path = ROOT / "tests" / "fixtures" / ("task_contract_failure_replies_20261113.json" if case == "wrong_url"
                                         else "browser_handoff_failure_replies_20261114.json")
    entry = decode_json_object(parse_json_object(path.read_text(encoding="utf-8"))["basic" if case == "wrong_url" else case])
    if case == "wrong_url":
        replies = entry["replies"]
        assert isinstance(replies, list)
        raw = require_json_string(replies[1], "saved wrong URL reply")
    else:
        raw = require_json_string(entry["reply"], "saved failure reply")
    return raw, {"fixture": str(path.relative_to(ROOT)), "source": entry["source"],
                 "source_sha256": entry["source_sha256"], "reply_sha256": hashlib.sha256(raw.encode()).hexdigest()}


def same_call(action: JsonObject, offered: JsonObject, snapshot: TurnSnapshot) -> bool:
    actual = normalize_turn(decode_text_turn(json.dumps(action)), snapshot=snapshot)
    expected = normalize_turn(decode_text_turn(json.dumps(offered)), snapshot=snapshot)
    return (not actual.protocol_error and not expected.protocol_error
            and [(call.name, call.args) for call in actual.calls] == [(call.name, call.args) for call in expected.calls])


def run_prefix(directory: Path, case: str, target: Target) -> JsonObject:
    raw, provenance = saved_prefix(case)
    code = decode_json_object(parse_json_object(raw)["args"])["code"]
    handoff = ""
    handoff_snapshot: TurnSnapshot | None = None
    original_snapshot = KernelContext.request_snapshot

    class SavedPrefixProvider(ExperimentProvider):
        def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                      *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                      tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                      tool_call_contract: str = "", tool_call_observations: str = "",
                      tool_call_history_groups: int = 0, output_token_limit: int = 0,
                      require_tool_call: bool = False) -> dict[str, object]:
            if self.exchange == 0:
                self.exchange = 1
                body: dict[str, object] = {"choices": [{"message": {
                    "role": "assistant", "content": raw, "tool_calls": []}, "finish_reason": "stop"}]}
                append_json(self.directory / "provider.jsonl", {"type": "scripted_failure_prefix",
                            "exchange": self.exchange, "payload": body, "provenance": provenance})
                return body
            return super()._generate(messages, tools, timeout=timeout, checkpoints=checkpoints,
                tool_call_json=tool_call_json, tool_call_schema=tool_call_schema, tool_call_contract=tool_call_contract,
                tool_call_observations=tool_call_observations, tool_call_history_groups=tool_call_history_groups,
                output_token_limit=output_token_limit, require_tool_call=require_tool_call)

    def capture(context: KernelContext, snapshot: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        nonlocal handoff, handoff_snapshot
        result = original_snapshot(context, snapshot, delivered_results=delivered_results)
        append_json(directory / "handoffs.jsonl", {"text": result[1], "delivered": [
            {"tool": row.call.name, "ok": row.ok, "error_code": row.audit.get("error_code"),
             "child_effect_ids": row.canonical.get("child_effect_ids")} for row in delivered_results]})
        if handoff_snapshot is None and any(row.call.name == "codemode" and row.call.args.get("code") == code
                                            for row in delivered_results):
            handoff = result[1]
            handoff_snapshot = result[0]
        return result

    with (patch("tools.browser_production_live_gate.ExperimentProvider", SavedPrefixProvider),
          patch.object(KernelContext, "request_snapshot", capture)):
        row = run_case(directory, "chat", target, download_file=case == "download")
    write_json(directory / "prefix-provenance.json", provenance)
    receipts = tool_receipts(directory / "state")
    prefix_index, prefix = next((index, receipt) for index, receipt in enumerate(receipts)
                               if receipt.result.call.name == "codemode" and receipt.result.call.args.get("code") == code)
    children = prefix.result.canonical.get("child_effect_ids")
    refs = frozenset(ref for ref in children if isinstance(ref, str)) if isinstance(children, list) else frozenset()
    after = [receipt.result for receipt in receipts[prefix_index + 1:]
             if receipt.identity not in refs and receipt.result.call.name.startswith("browser_")]
    actions = response_actions(directory / "provider.jsonl")
    offered = [parse_json_object(line.removeprefix("Execute this next step: ")) for line in handoff.splitlines()
               if line.startswith("Execute this next step: ")]
    calls = Counter(receipt.result.call.name for receipt in receipts)
    error_code = prefix.result.audit.get("error_code")
    summary: JsonObject = {
        "case": case, "passed": row.get("passed") is True, "stop_reason": str(row.get("stop_reason")),
        "model": target.model, "live_generations": len(actions), "prefix_ok": prefix.result.ok,
        "prefix_error_code": error_code if isinstance(error_code, str) else None, "prefix_handoff": handoff,
        "first_live_action": actions[0] if actions else None,
        "submitted_offered_call": (bool(actions and handoff_snapshot is not None and any(
            same_call(actions[0], action, handoff_snapshot) for action in offered)) if offered else None),
        "first_leaf_after_prefix": {"tool": after[0].call.name, "args": after[0].call.args} if after else None,
        "download_uses_prefix_snapshot": bool(after and after[0].call.name == "browser_download"
            and isinstance(ref := after[0].call.args.get("ref"), str) and ref in handoff) if case == "download" else None,
        "tool_calls": dict(calls), "provenance": provenance,
        "scope": "saved_failure_prefix_then_unmodified_local_model_continuation",
    }
    write_json(directory / "continuation-summary.json", summary)
    return summary


class Options(argparse.Namespace):
    run_dir: Path
    case: str
    timeout: float
    kobold_base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Options())
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = select_target(args.timeout, args.kobold_base_url)
    before = production_source_digest()
    rows: list[JsonObject] = []
    for case in CASES if args.case == "all" else (args.case,):
        print(json.dumps({"starting": case}), flush=True)
        result = run_prefix(root / case, case, target)
        rows.append(result)
        print(json.dumps({"case": case, "passed": result["passed"],
                          "submitted_offered_call": result["submitted_offered_call"],
                          "download_uses_prefix_snapshot": result["download_uses_prefix_snapshot"]}), flush=True)
    write_json(root / "summary.json", {"results": rows, "passed": sum(row["passed"] is True for row in rows),
        "runs": len(rows), "source_before": before, "source_after": production_source_digest(),
        "limits": "A saved failure prefix is scripted; these small continuation samples do not measure first-attempt success rates."})
    return 0 if all(row["passed"] is True for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
