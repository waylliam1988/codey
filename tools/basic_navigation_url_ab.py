"""Experimental task-URL selector over the production basic browser gate.

A retains browser_navigate({url}). B advertises {target:'task'} and lowers that
choice to the original task URL only when the user code executes the call.
The guest adapter is local to this harness. Production authority, browser effects,
receipts and completion are unchanged; model replies are never repaired live.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol
from unittest.mock import patch

from codey.codemode import catalog as codemode_catalog
from codey.codemode.runtime import NodeWorkerRuntime, ToolInvoker
from codey.codemode.types import CodeModeLimits, CodeModeResult, ToolCatalogEntry, ToolCatalogSnapshot
from codey.policies.network import check_fetch_url
from codey.task.web_intent import explicit_web_intent, source_url_key
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer
from tools.browser_production_live_gate import run_case, select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target, append_json, production_source_digest, provider_metrics, write_json

Arm = Literal["A", "B"]
_RAW_URL = re.compile(r"https?://[^\s<>`\"'、，。；！？（）\[\]]+", re.IGNORECASE)
SELECTOR: JsonObject = {"type": "object", "properties": {"target": {"type": "string", "enum": ["task"]}},
                        "required": ["target"], "additionalProperties": False}
SUFFIX = "\n})();\n})(tools);"


class RuntimeExecute(Protocol):
    def __call__(self, runtime: NodeWorkerRuntime, code: str, /, *, catalog: Sequence[ToolCatalogEntry],
                 invoke: ToolInvoker, inputs: Mapping[str, object] | None = None, state_store: object = None,
                 replay_context: Mapping[str, object] | None = None,
                 limits: CodeModeLimits | None = None) -> CodeModeResult: ...


@dataclass(frozen=True)
class TaskDestination:
    url: str

    @classmethod
    def from_task(cls, task: str) -> TaskDestination:
        intent = explicit_web_intent(task)
        if not intent.requested or len(intent.urls) != 1:
            raise ValueError("Task navigation requires one explicit authorized destination.")
        originals = {match[0] for match in _RAW_URL.finditer(task) if source_url_key(match[0]) in intent.urls}
        if len(originals) != 1:
            raise ValueError("The original task URL is missing or ambiguous; do not normalize or guess it.")
        url = next(iter(originals))
        if check_fetch_url(url, resolve=False):
            raise ValueError("The task destination is not eligible for navigation.")
        return cls(url)

    @property
    def helper(self) -> str:
        literal = json.dumps(self.url, ensure_ascii=True)
        return """return await (async (hostTools) => {
const tools = new Proxy(hostTools, {get(target, property) {
  if (property !== 'browser_navigate') return Reflect.get(target, property);
  return async (args) => {
    if (!args || typeof args !== 'object' || Array.isArray(args)
        || Object.keys(args).length !== 1 || args.target !== 'task') {
      throw Error("Use browser_navigate({target:'task'}) to open the task's one authorized page. Do not pass or copy a URL.");
    }
    return await hostTools.browser_navigate({url:""" + literal + """});
  };
}});
return await (async () => {
"""


def selector_catalog(snapshot: ToolCatalogSnapshot) -> ToolCatalogSnapshot:
    return replace(snapshot, entries=tuple(replace(entry, parameters=SELECTOR,
        description="Navigate when needed to the task's one explicitly authorized page. Select target=task; the runtime supplies its original URL.")
        if entry.name == "browser_navigate" else entry for entry in snapshot.entries))


@contextmanager
def navigation_surface(directory: Path, destination: TaskDestination, *, enabled: bool) -> Generator[None]:
    execute: RuntimeExecute = NodeWorkerRuntime.execute
    digest = NodeWorkerRuntime.contract_digest
    declarations = codemode_catalog.render_declarations
    helper = destination.helper

    def render(snapshot: ToolCatalogSnapshot) -> str:
        return declarations(selector_catalog(snapshot))

    def bound_digest(runtime: NodeWorkerRuntime) -> str:
        return hashlib.sha256((digest(runtime) + ":task-navigation-v1:" + helper + SUFFIX).encode()).hexdigest()

    def recorded_execute(runtime: NodeWorkerRuntime, code: str, *, catalog: Sequence[ToolCatalogEntry],
                         invoke: ToolInvoker, inputs: Mapping[str, object] | None = None, state_store: object = None,
                         replay_context: Mapping[str, object] | None = None,
                         limits: CodeModeLimits | None = None) -> CodeModeResult:
        expanded = helper + code + SUFFIX if enabled else code
        append_json(directory / "programs.jsonl", {"model_code": code, "executed_code": expanded,
                    "inputs": decode_json_object(inputs) if inputs is not None else None,
                    "selector_enabled": enabled, "bound_url": destination.url})

        def observed_invoke(name: str, args: dict[str, object]) -> object:
            if name == "browser_navigate":
                append_json(directory / "navigation-intents.jsonl", {"tool": name,
                    "args": decode_json_object(args), "selector_enabled": enabled})
            return invoke(name, args)

        return execute(runtime, expanded, catalog=catalog, invoke=observed_invoke, inputs=inputs,
                       state_store=state_store, replay_context=replay_context, limits=limits)

    with ExitStack() as stack:
        stack.enter_context(patch.object(NodeWorkerRuntime, "execute", recorded_execute))
        if enabled:
            stack.enter_context(patch.object(NodeWorkerRuntime, "contract_digest", bound_digest))
            stack.enter_context(patch.object(codemode_catalog, "render_declarations", render))
        yield


def navigation_audit(directory: Path, destination: TaskDestination) -> JsonObject:
    receipts = tool_receipts(directory / "state")
    navigations: list[JsonValue] = []
    stages: dict[str, list[str]] = {name: [] for name in (
        "browser_snapshot", "browser_fill", "browser_click", "browser_wait", "browser_read")}
    order = ("browser_snapshot", "browser_fill", "browser_snapshot", "browser_click", "browser_wait", "browser_read")
    partial: list[str] = []
    completed: list[JsonValue] = []
    for row in receipts:
        result = row.result
        name = result.call.name
        value = decode_json_object(result.canonical).get("value")
        data: JsonObject = decode_json_object(value) if isinstance(value, dict) else {}
        if result.call.name == "browser_navigate":
            navigations.append({"ref": row.identity, "ok": result.ok, "args": result.call.args,
                "http_status": data.get("http_status"), "observed_url": data.get("url"),
                "exact_task_url": result.call.args.get("url") == destination.url})
            partial = [row.identity] if result.ok and result.call.args.get("url") == destination.url and data.get("http_status") == 200 else []
            continue
        if name in {"browser_tab_new", "browser_tab_switch", "browser_tab_close"}:
            partial = []
        elif result.ok and result.call.name in stages:
            stages[result.call.name].append(row.identity)
        if not partial or not result.ok or name != order[len(partial) - 1]:
            continue
        if name == "browser_wait" and (result.call.args.get("condition") != "text"
                or result.call.args.get("value") != "鱼丸" or data.get("matched") is not True
                or data.get("page_url") != destination.url):
            continue
        if name == "browser_read":
            observation = decode_json_object(result.canonical["research_observation"])
            source = decode_json_object(observation["source"])
            body = observation.get("text")
            if (result.truncated or source.get("truncated") is not False or source.get("final_url") != destination.url
                    or not isinstance(body, str) or not re.search(r"(?<!\d)173(?!\d)", body) or "QUARTZ_851" not in body):
                continue
        partial.append(row.identity)
        if len(partial) == len(order) + 1:
            completed.append(list[JsonValue](partial))
            partial = []
    correct = [decode_json_object(row) for row in navigations]
    return {"formal_navigations": navigations,
        "all_navigations_exact": bool(correct) and all(row["exact_task_url"] is True for row in correct),
        "target_loaded": any(row["exact_task_url"] is True and row["http_status"] == 200
                             and row["observed_url"] == destination.url for row in correct),
        "wrong_navigation_count": sum(row["exact_task_url"] is False for row in correct),
        "successful_stage_refs": {name: list(refs) for name, refs in stages.items()},
        "completed_chain_refs": completed, "browser_chain_reached": bool(completed)}


def run_arm(directory: Path, arm: Arm, target: Target, *, max_turns: int = 6) -> JsonObject:
    captured: list[TaskDestination] = []
    with ExitStack() as contexts:
        def task(text: str) -> str:
            destination = TaskDestination.from_task(text)
            captured.append(destination)
            contexts.enter_context(navigation_surface(directory, destination, enabled=arm == "B"))
            write_json(directory / "navigation-contract.json", {"original_task": text, "url": destination.url,
                "arm": arm, "parameters": SELECTOR if arm == "B" else {"url": {"type": "string"}}})
            return text + ("导航接口实验：tools.browser_navigate 只接收 {target:'task'}；"
                           "你决定何时打开任务指定页面，程序原样传入任务 URL。不要传 url。" if arm == "B" else "")

        result = decode_json_object(run_case(directory, "chat", target, max_turns=max_turns, task_transform=task))
    if len(captured) != 1:
        raise ValueError("Expected one frozen task destination")
    audit = navigation_audit(directory, captured[0])
    result.update(gate_passed=result["passed"], passed=result["passed"] is True and audit["browser_chain_reached"] is True,
                  arm=arm, seed=target.seed, navigation=audit, **provider_metrics(directory / "provider.jsonl"))
    write_json(directory / "summary.json", result)
    return result


class Options(argparse.Namespace):
    run_dir: Path
    pairs: int
    max_turns: int
    base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Options())
    if args.pairs < 1 or args.max_turns < 1:
        parser.error("pairs and max-turns must be positive")
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = select_target(120, args.base_url)
    before = production_source_digest()
    rows: list[JsonValue] = []
    for pair in range(args.pairs):
        current = replace(target, seed=target.seed + pair)
        arms: tuple[Arm, ...] = ("A", "B") if pair % 2 == 0 else ("B", "A")
        for arm in arms:
            progress: JsonObject = {"pair": pair + 1, "arm": arm}
            print(json.dumps(progress), flush=True)
            row = run_arm(root / f"pair-{pair + 1}" / arm, arm, current, max_turns=args.max_turns)
            rows.append(row)
            status: JsonObject = {key: row[key] for key in ("arm", "passed", "stop_reason", "navigation")}
            print(json.dumps(status), flush=True)
            write_json(root / "progress.json", rows)
    after = production_source_digest()
    write_json(root / "summary.json", {"scope": "guest_navigation_selector_production_basic_gate",
        "model": target.model, "production_before": before, "production_after": after,
        "production_unchanged": before == after, "results": rows,
        "model_calls": sum(require_json_integer(decode_json_object(row)["model_calls"], "model_calls") for row in rows)})
    return 0 if before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
