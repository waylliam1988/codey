"""Run a fixed conflicting-evidence and calculation task with the local model."""
from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from codey.policies.tool_requirement import require_tool_success
from codey.providers.base import ProviderToolDefinition
from codey.research.passages import read_passage_refs
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from tools import research_query_delivery_live_gate as gate
from tools.browser_experiment_sources import ResumableBrowserSearchProvider as BrowserSearchProvider
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, append_json, write_json

CEDAR = "https://example.com/followup-cedar"
BIRCH = "https://example.com/followup-birch"
CASE = gate.QueryCase("/followup-cedar", (
    "Compare Atlas fuel consumption for method A versus method B at 20 °C and 10 L workload. "
    "Save both findings using save_finding. In each comparison use subject Atlas, factor method A, comparator method B, "
    "endpoint fuel consumption, uncertainty unknown, and direction higher or lower as observed. "
    "Use the same reported conditions in both saves. Then use source_search to inspect 'benchmark' in the Cedar source. "
    "In your report compare method A's readings with the explicit calculation 7 kg (31 kg - 24 kg) and cite both sources "
    "immediately after that expression. Use submit_research_answer."
), "<h1>Cedar benchmark</h1><p>At 20 °C and 10 L workload, the Atlas unit consumed 24 kg with method A and 27 kg with method B. "
   "Method A used less fuel.</p>", ("24 kg", "31 kg", "7 kg"), additional_pages=((BIRCH,
       "<h1>Birch benchmark</h1><p>At 20 °C and 10 L workload, the Atlas unit consumed 31 kg with method A and 27 kg with method B. "
       "Method A used more fuel.</p>"),), required_tools=tuple(require_tool_success("save_finding", arguments={
           "comparison": {"subject": "Atlas", "factor": "method A", "comparator": "method B",
               "endpoint": "fuel consumption", "direction": direction, "uncertainty": "unknown"}})
           for direction in ("lower", "higher")))


class Options(argparse.Namespace):
    directory: Path
    base_url: str
    prepared: bool


def preparation(directory: Path) -> Iterator[JsonObject]:
    for url in (CEDAR, BIRCH):
        if url not in _read_refs(directory):
            yield {"tool": "open_url", "args": {"target": {"url": url}}}
    for url, direction, fuel in ((CEDAR, "lower", 24), (BIRCH, "higher", 31)):
        yield {"tool": "save_finding", "args": {"passage_ref": _read_refs(directory)[url],
            "claim": f"Atlas method A consumed {fuel} kg under the fixed workload.",
            "comparison": {"subject": "Atlas", "factor": "method A", "comparator": "method B",
                "endpoint": "fuel consumption", "direction": direction, "uncertainty": "unknown"}}}
    yield {"tool": "source_search", "args": {"target": {"url": CEDAR}, "query": "benchmark"}}


def _read_refs(directory: Path) -> dict[str, str]:
    return {url: read_passage_refs(row.result.canonical["read_passages"])[0]
            for row in tool_receipts(directory / "state") if row.result.call.name == "open_url" and row.result.ok
            and row.result.canonical.get("read_passages") and isinstance(url := row.result.call.args.get("url"), str)}


class PreparedResearchProvider(ExperimentProvider):
    _preparation: Iterator[JsonObject] | None = None

    def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                  *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                  tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                  tool_call_contract: str = "", tool_call_observations: str = "", tool_call_history_groups: int = 0,
                  output_token_limit: int = 0, require_tool_call: bool = False) -> dict[str, object]:
        if self._preparation is None:
            self._preparation = preparation(self.directory)
        action = next(self._preparation, None)
        if action is not None:
            append_json(self.directory / "preparation.jsonl", {"type": "scripted_preparation", "action": action})
            return {"choices": [{"message": {"role": "assistant", "content": json.dumps(action),
                      "tool_calls": []}, "finish_reason": "stop"}]}
        return super()._generate(messages, tools, timeout=timeout, checkpoints=checkpoints, tool_call_json=tool_call_json,
            tool_call_schema=tool_call_schema, tool_call_contract=tool_call_contract, tool_call_observations=tool_call_observations,
            tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit, require_tool_call=require_tool_call)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--prepared", action="store_true", help="Supply fixed evidence calls before the real model writes the answer")
    options = parser.parse_args(namespace=Options())
    target = select_target(240, options.base_url)
    with (patch.object(BrowserSearchProvider, "search", return_value=[{"title": "Cedar benchmark", "url": CEDAR,
            "snippet": "Atlas fuel consumption benchmark"}]),
          patch.object(gate, "ExperimentProvider", PreparedResearchProvider if options.prepared else ExperimentProvider)):
        outcome = gate.run_case(options.directory, CASE, target, max_turns=12, source_access_wording=True)
    receipts = tool_receipts(options.directory / "state")
    searches = [row.result for row in receipts if row.result.call.name == "web_search" and row.result.ok]
    findings = [row.result for row in receipts if row.result.call.name == "save_finding" and row.result.ok]
    comparisons = [row for row in findings if isinstance(row.call.args.get("comparison"), dict)]
    directions = {require_json_string(decode_json_object(row.call.args["comparison"]).get("direction"), "direction")
                  for row in comparisons}
    refs = _read_refs(options.directory)
    source_bindings = all(row.call.args.get("passage_ref") == refs.get(
        CEDAR if decode_json_object(row.call.args["comparison"]).get("direction") == "lower" else BIRCH)
        for row in comparisons)
    calculations = [decode_json_object(row.result.canonical["research_evidence_check"]).get("verified_calculations")
                    for row in receipts if row.result.ok and "research_evidence_check" in row.result.canonical]
    passed = (outcome["passed"] is True and len(comparisons) == 2 and directions == {"lower", "higher"} and source_bindings
              and len(searches) == 1 and not searches[0].call.call_id and any(calculations)
              and isinstance(calls := outcome.get("model_calls"), int) and calls > 0)
    write_json(options.directory / "followup-summary.json", {"passed": passed, "stop_reason": outcome["stop_reason"],
        "findings": len(findings), "comparisons": len(comparisons), "unscoped_findings": len(findings) - len(comparisons),
        "comparison_sources_match": source_bindings,
        "searches": [row.call.args for row in searches], "calculations": calculations,
        "source_stable": outcome["source_stable"], "prepared": options.prepared, "model_calls": outcome.get("model_calls")})
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
