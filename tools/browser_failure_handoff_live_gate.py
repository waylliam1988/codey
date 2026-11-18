"""Replay a saved failure prefix, then let the local model use production handoffs.

Only the saved prefix is scripted. Tool contracts, execution, receipts,
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
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    parse_json_object,
    require_json_integer,
    require_json_string,
)
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
CASES = ("basic", "download", "wrong_url", "partial", "observation")


def saved_prefix(case: str) -> tuple[str, JsonObject]:
    if case == "observation":
        code = (
            "const s=unwrap(await tools.browser_snapshot({}));let field='';let button='';"
            "for(const e of s.elements){if(e.name==='Locality'){field=e.ref;break;}"
            "if(e.name==='Load record'){button=e.ref;break;}}return {field,button};"
        )
        observation_action: JsonObject = {"tool": "codemode", "args": {"code": code, "inputs": {}}}
        raw = json.dumps(observation_action, ensure_ascii=False)
        return raw, {"source": "two scripted observation-only early-break programs before live continuation",
                     "reply_sha256": hashlib.sha256(raw.encode()).hexdigest(), "scripted_generations": 2}
    if case == "partial":
        code = (
            "const observed=unwrap(await tools.browser_snapshot({}));"
            "const field=observed.elements.find(e=>e.role==='textbox'&&e.name==='Locality');"
            "unwrap(await tools.browser_fill({ref:field.ref,value:'福州'}));"
            "const current=await tools.browser_snapshot({});return current.elements;"
        )
        action: JsonObject = {"tool": "codemode", "args": {"code": code, "inputs": {}}}
        raw = json.dumps(action, ensure_ascii=False)
        provenance: JsonObject = {"source": "explicit partial-success Result<T> failure injection",
                                  "reply_sha256": hashlib.sha256(raw.encode()).hexdigest()}
        return raw, provenance
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


def observation_recovery_requests(path: Path) -> list[int]:
    rows = [parse_json_object(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [require_json_integer(row["exchange"], "exchange") for row in rows
            if row.get("type") == "wire_attempt" and row.get("phase") == "request"
            and "only observed control refs changed" in json.dumps(row["payload"], ensure_ascii=False)]


def run_prefix(directory: Path, case: str, target: Target) -> JsonObject:
    raw, provenance = saved_prefix(case)
    code = decode_json_object(parse_json_object(raw)["args"])["code"]
    handoff = ""
    handoff_snapshot: TurnSnapshot | None = None
    delivered_prefixes = 0
    original_snapshot = KernelContext.request_snapshot
    prefix_count = 2 if case == "observation" else 1

    class SavedPrefixProvider(ExperimentProvider):
        def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                      *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                      tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                      tool_call_contract: str = "", tool_call_observations: str = "",
                      tool_call_history_groups: int = 0, output_token_limit: int = 0,
                      require_tool_call: bool = False) -> dict[str, object]:
            if self.exchange < prefix_count:
                self.exchange += 1
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
        nonlocal handoff, handoff_snapshot, delivered_prefixes
        result = original_snapshot(context, snapshot, delivered_results=delivered_results)
        append_json(directory / "handoffs.jsonl", {"text": result[1], "delivered": [
            {"tool": row.call.name, "ok": row.ok, "error_code": row.audit.get("error_code"),
             "child_effect_ids": row.canonical.get("child_effect_ids")} for row in delivered_results]})
        delivered_prefixes += sum(row.call.name == "codemode" and row.call.args.get("code") == code
                                  for row in delivered_results)
        if handoff_snapshot is None and delivered_prefixes >= prefix_count:
            handoff = result[1]
            handoff_snapshot = result[0]
        return result

    with (patch("tools.browser_production_live_gate.ExperimentProvider", SavedPrefixProvider),
          patch.object(KernelContext, "request_snapshot", capture)):
        row = run_case(directory, "chat", target, download_file=case == "download",
                       codemode_only=case in {"partial", "observation"})
    write_json(directory / "prefix-provenance.json", provenance)
    receipts = tool_receipts(directory / "state")
    prefixes = [(index, receipt) for index, receipt in enumerate(receipts)
                if receipt.result.call.name == "codemode" and receipt.result.call.args.get("code") == code]
    prefix_index, prefix = prefixes[min(prefix_count, len(prefixes)) - 1]
    children = prefix.result.canonical.get("child_effect_ids")
    refs = frozenset(ref for ref in children if isinstance(ref, str)) if isinstance(children, list) else frozenset()
    after = [receipt.result for receipt in receipts[prefix_index + 1:]
             if receipt.identity not in refs and receipt.result.call.name.startswith("browser_")]
    actions = response_actions(directory / "provider.jsonl")
    offered = [parse_json_object(line.removeprefix("Execute this next step: ")) for line in handoff.splitlines()
               if line.startswith("Execute this next step: ")]
    calls = Counter(receipt.result.call.name for receipt in receipts)
    error_code = prefix.result.audit.get("error_code")
    partial_recovered = (case != "partial" or error_code == "result_contract" and calls["browser_fill"] == 1
                         and bool(after) and after[0].call.name != "browser_navigate"
                         and "Current controls from settled snapshot" in handoff)
    recovery_requests = observation_recovery_requests(directory / "provider.jsonl")
    recovery_offered = bool(recovery_requests)
    observation_recovered = (case != "observation" or len(prefixes) == prefix_count and recovery_offered
                             and calls["browser_fill"] == 1 and calls["browser_click"] == 1
                             and calls["browser_navigate"] == 1 and calls["browser_read"] >= 1)
    summary: JsonObject = {
        "case": case, "passed": row.get("passed") is True and partial_recovered and observation_recovered,
        "stop_reason": str(row.get("stop_reason")),
        "model": target.model, "live_generations": len(actions), "prefix_ok": prefix.result.ok,
        "prefix_error_code": error_code if isinstance(error_code, str) else None, "prefix_handoff": handoff,
        "first_live_action": actions[0] if actions else None,
        "submitted_offered_call": (bool(actions and handoff_snapshot is not None and any(
            same_call(actions[0], action, handoff_snapshot) for action in offered)) if offered else None),
        "first_leaf_after_prefix": {"tool": after[0].call.name, "args": after[0].call.args} if after else None,
        "download_uses_prefix_snapshot": bool(after and after[0].call.name == "browser_download"
            and isinstance(ref := after[0].call.args.get("ref"), str) and ref in handoff) if case == "download" else None,
        "tool_calls": dict(calls), "provenance": provenance,
        "partial_success_recovered_without_repeating_fill": (
            row.get("passed") is True and partial_recovered if case == "partial" else None),
        "scripted_generations": prefix_count, "continuation_after_receipt_index": prefix_index,
        "observation_recovery_offered": recovery_offered if case == "observation" else None,
        "observation_recovery_request_exchanges": list(recovery_requests),
        "observation_recovered_without_repeating_actions": (
            row.get("passed") is True and observation_recovered if case == "observation" else None),
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
        starting: JsonObject = {"starting": case}
        print(json.dumps(starting), flush=True)
        result = run_prefix(root / case, case, target)
        rows.append(result)
        progress: JsonObject = {"case": case, "passed": result["passed"],
                               "submitted_offered_call": result["submitted_offered_call"],
                               "download_uses_prefix_snapshot": result["download_uses_prefix_snapshot"]}
        print(json.dumps(progress), flush=True)
    summary: JsonObject = {"results": list(rows), "passed": sum(row["passed"] is True for row in rows),
        "runs": len(rows), "source_before": before, "source_after": production_source_digest(),
        "limits": "A saved failure prefix is scripted; these small continuation samples do not measure first-attempt success rates."}
    write_json(root / "summary.json", summary)
    return 0 if all(row["passed"] is True for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
