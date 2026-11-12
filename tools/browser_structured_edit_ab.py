"""Compare fixed browser capture and structured edits with model-written CodeMode."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from codey.policies.tool_requirement import decode_answer
from codey.providers.base import ProviderToolDefinition
from codey.providers.text_tool_schema import text_tool_schema
from codey.toolchain.result_schema import validate_output
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import browser_field_binding_probe as probe
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    write_json,
)
from tools.structured_surface import object_schema

STRING: JsonObject = {"type": "string"}
VALUE_TYPE: JsonObject = {"type": "string", "enum": ["integer", "string"]}
OBSERVE = object_schema({"url": STRING, "format": {"type": "string", "enum": ["table", "json"]}}, ["url", "format"])
TABLE = object_schema({"id": STRING, "key_column": STRING, "key": STRING, "column": STRING, "type": VALUE_TYPE},
                      ["id", "key_column", "key", "column", "type"])
JSON = object_schema({"id": STRING, "pointer": STRING, "type": VALUE_TYPE}, ["id", "pointer", "type"])
DONE = object_schema({"summary": STRING}, ["summary"])
OBSERVE_CODE = (
    "unwrap(await tools.browser_navigate({url:inputs.url}));"
    "const capture=await tools.browser_eval({code:inputs.projection});"
    "const data=unwrap(capture).value;await store('source',capture.result_ref);return data;"
)
EDIT_CODE = (
    "const read=await tools.read_file({path:inputs.file});"
    "const ref=await load('source');"
    "return unwrap(await tools.edit({read,changes:{[inputs.field]:{...inputs.selector,ref}}}));"
)


def tool_for(case: probe.Case, phase: str, observed_ids: tuple[str, ...]) -> ProviderToolDefinition:
    if phase == "observe":
        return ProviderToolDefinition("observe", "Read the authorized page first. args: {url, format}; format is table or json.", OBSERVE)
    if phase == "done":
        return ProviderToolDefinition("done", "The file edit succeeded. Report it with args: {summary}.", DONE)
    fields: list[JsonValue] = [key for key in case.expected]
    destination = object_schema(
        {"file": {"type": "string", "enum": [case.path]}, "field": {"type": "string", "enum": fields}},
        ["file", "field"],
    )
    source, locator = ((TABLE, "{id,key_column,key,column,type}") if phase == "table"
                       else (JSON, "{id,pointer,type}, with JSON Pointer /a/b"))
    ids: list[JsonValue] = list(observed_ids)
    source = {**source, "properties": {**decode_json_object(source["properties"]),
                                      "id": {"type": "string", "enum": ids}}}
    schema = object_schema({"source": source, "target": destination}, ["source", "target"])
    return ProviderToolDefinition("edit",
        f"Copy the value from the observed data. args.source is {locator}. "
        f"source.id is one of these page IDs: {json.dumps(ids)}. "
        f"args.target is {{file,field}}, file is {case.path}, field is one of {json.dumps(fields)}. "
        "Use a bare file field name, separate from the source locator. Repeat the edit after a version conflict.",
        schema)


def lower(action: JsonObject, case: probe.Case, phase: str, observed_ids: tuple[str, ...] = ()) -> JsonObject:
    validate_output("action", object_schema({"tool": STRING, "args": {"type": "object"}}, ["tool", "args"]), action)
    name, args = action["tool"], decode_json_object(action["args"])
    offered = tool_for(case, phase, observed_ids)
    if name != offered.name:
        raise ValueError(f"The only available action is {offered.name}. {offered.description}")
    validate_output(offered.name, offered.parameters, args)
    if name == "observe":
        if args["url"] != case.url:
            raise ValueError("URL differs from the task's authorized fixture")
        return {"tool": "codemode", "args": {"code": OBSERVE_CODE, "inputs": {
            "url": args["url"], "projection": f"receipt:{args['format']}"}}}
    if name == "edit":
        source, target = decode_json_object(args["source"]), decode_json_object(args["target"])
        return {"tool": "codemode", "args": {"code": EDIT_CODE, "inputs": {
            "file": target["file"], "field": target["field"], "selector": source}}}
    return action


class StructuredProvider(ExperimentProvider):
    def __init__(self, target: Target, directory: Path, case: probe.Case) -> None:
        super().__init__(target, directory, fixed_acquisition=False)
        self.case = case
        self.phase = "observe"
        self.observed_ids: tuple[str, ...] = ()

    def current_observation(self) -> tuple[str, tuple[str, ...]]:
        state = self.directory / "state"
        if not (state / "durable.sqlite3").exists():
            return "observe", ()
        receipts = tool_receipts(state)
        committed = {child for row in receipts if row.result.call.name == "codemode" and row.result.ok
                     and isinstance(children := row.result.canonical.get("child_effect_ids"), list)
                     for child in children if isinstance(child, str)}
        phase = "observe"
        ids: tuple[str, ...] = ()
        for row in receipts:
            result = row.result
            if row.identity not in committed or not result.ok:
                continue
            if result.call.name == "edit" and result.call.args.get("path") == self.case.path:
                return "done", ()
            if result.call.name == "browser_eval":
                value = result.canonical.get("value")
                data = value.get("value") if isinstance(value, dict) and value.get("truncated") is False else None
                counts = Counter(item["id"] for item in data
                                 if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]) \
                    if isinstance(data, list) else Counter()
                ids = tuple(key for key, count in counts.items() if count == 1)
                code = result.call.args.get("code")
                phase = ("table" if code == probe.TABLE else "json") if ids and code in (probe.TABLE, probe.STRUCTURED) else "observe"
        return phase, ids

    def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                  *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                  tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                  tool_call_contract: str = "", tool_call_observations: str = "",
                  tool_call_history_groups: int = 0, output_token_limit: int = 0,
                  require_tool_call: bool = False) -> dict[str, object]:
        self.phase, self.observed_ids = self.current_observation()
        offered = tool_for(self.case, self.phase, self.observed_ids)
        contract = f"Submit one JSON object with tool and args. The only available tool is {offered.name}. {offered.description}"
        return super()._generate(messages, tools, timeout=timeout, checkpoints=checkpoints,
            tool_call_json=tool_call_json, tool_call_schema=text_tool_schema([offered]),
            tool_call_contract=contract, tool_call_observations=tool_call_observations,
            tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit,
            require_tool_call=require_tool_call)

    def normalize_reply(self, reply: str) -> object:
        normalized = super().normalize_reply(reply)
        if not isinstance(normalized, str):
            return normalized
        original: JsonObject = {}
        error = ""
        try:
            original = decode_json_object(decode_answer(normalized))
            action = lower(original, self.case, self.phase, self.observed_ids)
        except (ValueError, TypeError, KeyError) as exc:
            error = str(exc)
            action = {"tool": "codemode", "args": {"code": "throw Error(inputs.reason);",
                "inputs": {"reason": f"Structured action rejected: {error}. "
                    f"{tool_for(self.case, self.phase, self.observed_ids).description}"}}}
        append_json(self.directory / "structured-actions.jsonl",
                    {"exchange": self.exchange, "phase": self.phase, "original": original, "lowered": action, "error": error})
        return json.dumps(action, ensure_ascii=False)


def run_arm(directory: Path, name: str, arm: str, target: Target, *, max_turns: int) -> JsonObject:
    case = probe.CASES[name]
    if arm == "A":
        return probe.run_case(directory, case, target, max_turns=max_turns)
    task = (f"打开 {case.url}。{case.task} 保留其他字段和 backup.json。不要运行测试或命令。"
            "先用 observe 读取页面，再用 edit 从观察结果写入文件。")
    row = probe.run_case(directory, case, target, max_turns=max_turns, task_override=task,
                         provider_factory=lambda provider, root: StructuredProvider(provider, root, case))
    log = directory / "structured-actions.jsonl"
    actions = [decode_json_object(json.loads(line))
               for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    edits = [action for action in actions if isinstance(original := action.get("original"), dict)
             and original.get("tool") == "edit" and not action["error"]]
    row["structured_edits"] = len(edits)
    row["passed"] = row["passed"] is True and len(edits) >= 2
    write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--max-turns", type=int, default=8)
    args = parser.parse_args()
    target = select_target(90, args.base_url)
    root = args.run_dir.resolve()
    root.mkdir(parents=True)
    source = production_source_digest()
    scripts = [Path(__file__), Path(probe.__file__)]
    hashes: list[JsonValue] = [hashlib.sha256(path.read_bytes()).hexdigest() for path in scripts]
    runs: list[JsonValue] = []
    for name, order in (("table", "AB"), ("json", "BA")):
        for arm in order:
            print(f"Starting {name}/{arm}", flush=True)
            row = run_arm(root / f"{name}-{arm}", name, arm, target, max_turns=args.max_turns)
            runs.append({**row, "case": name, "arm": arm})
            print(f"{name}/{arm}: passed={row['passed']} stop={row['stop_reason']} calls={row['model_calls']}", flush=True)
            write_json(root / "progress.json", {"runs": runs})
    stable = source == production_source_digest()
    scripts_stable = hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in scripts]
    write_json(root / "summary.json", {"runs": runs, "source": source, "source_stable": stable,
        "scripts_stable": scripts_stable, "scripts": hashes, "model": target.model,
        "seed": target.seed, "temperature": 0, "max_turns": args.max_turns})
    return 0 if stable and scripts_stable and all(isinstance(row, dict) and row["passed"] is True for row in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
