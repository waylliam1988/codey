"""Test task-authorized structured edits, anonymous tables and receipt recovery."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from codey.policies.tool_requirement import decode_answer
from codey.providers.base import ProviderToolDefinition
from codey.providers.text_tool_schema import text_tool_schema
from codey.toolchain.constants import STALE_READ_REFERENCE_ERROR
from codey.toolchain.result_schema import validate_output
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import browser_field_binding_probe as probe
from tools import browser_structured_edit_ab as previous
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    write_json,
)
from tools.structured_surface import object_schema

ANONYMOUS = probe.TABLE.replace("t=>({id:t.id,", '(t,i)=>({id:"table-"+(i+1),title:t.caption?.innerText.trim()||"",')
GRANT = re.compile(r"授权：从 (https://[^ ]+) 读取，只写 ([A-Za-z][\w-]*\.json) 的 (\w+)，类型 (integer|string)。", re.ASCII)
OBSERVE_CODE = (
    "unwrap(await tools.browser_navigate({url:inputs.url}));"
    "const table=await tools.browser_eval({code:'receipt:table'});"
    "const json=await tools.browser_eval({code:'receipt:json'});"
    "const t=unwrap(table),j=unwrap(json);"
    "if(t.truncated||j.truncated||Boolean(t.value.length)===Boolean(j.value.length))"
    "throw Error('Expected exactly one complete supported source format');"
    "const capture=t.value.length?table:json;"
    "await store('source',capture.result_ref);return unwrap(capture).value;"
)


@dataclass(frozen=True)
class Grant:
    url: str
    file: str
    field: str
    value_type: str

    @classmethod
    def from_task(cls, task: str) -> Grant:
        lines = [line for line in task.splitlines() if line.startswith("授权：")]
        match = GRANT.fullmatch(lines[0]) if len(lines) == 1 else None
        if match is None:
            raise ValueError("Expected one explicit file, field and type grant in the task")
        return cls(*match.groups())


TABLE_CASE = probe.Case("https://example.com/market-ledger", "ledger.json",
    "<table><caption>Wholesale</caption><thead><tr><th>Lot</th><th>Payable</th></tr></thead>"
    "<tbody><tr><td>B-17</td><td>9100</td></tr></tbody></table>"
    "<table><caption>Retail</caption><thead><tr><th>Lot</th><th>Payable</th></tr></thead>"
    "<tbody><tr><td>B-11</td><td>317</td></tr><tr><td>B-17</td><td>962</td></tr></tbody></table>",
    "授权：从 https://example.com/market-ledger 读取，只写 ledger.json 的 payable_cents，类型 integer。\n"
    "取 Retail 表的 B-17 批次 Payable 金额（整数分）。", {"payable_cents": 962}, 'tools.browser_eval({code:"receipt:table"})')
JSON_CASE = probe.Case("https://example.com/dispatch", "dispatch.json",
    '<script id="dispatch" type="application/json">{"parcel":{"label":"000783"},"billing":{"label":"9937"}}</script>'
    "<article>Dispatch record ready. The parcel label is separate from the billing reference. "
    "Keep the carrier label in its original string format when updating dispatch records.</article>",
    "授权：从 https://example.com/dispatch 读取，只写 dispatch.json 的 carrier_label，类型 string。\n"
    "复制网页 JSON 中 parcel 的 label，保留前导零。", {"carrier_label": "000783"}, 'tools.browser_eval({code:"receipt:json"})')
CASES = {"anonymous": TABLE_CASE, "typed_json": JSON_CASE, "expired": JSON_CASE}


@dataclass(frozen=True)
class Step:
    phase: str = "observe"
    ids: tuple[str, ...] = ()
    context: str = ""
    capture: EffectReceipt | None = None


def next_step(directory: Path, grant: Grant) -> Step:
    state = directory / "state"
    if not (state / "durable.sqlite3").exists():
        return Step()
    receipts = tool_receipts(state)
    committed = {child for row in receipts if row.result.call.name == "codemode" and row.result.ok
                 and isinstance(children := row.result.canonical.get("child_effect_ids"), list)
                 for child in children if isinstance(child, str)}
    capture = None
    conflict = False
    for row in receipts:
        result = row.result
        if result.call.name == "edit" and result.call.args.get("path") == grant.file:
            if row.identity in committed and result.ok:
                return Step("done")
            conflict = not result.ok and STALE_READ_REFERENCE_ERROR in result.model_text
        elif row.identity in committed and result.call.name == "browser_eval" and result.ok:
            value = result.canonical.get("value")
            if isinstance(value, dict) and value.get("truncated") is False and value.get("value"):
                capture, conflict = row, False
    if capture is None:
        return Step()
    log = directory / "bindings.jsonl"
    last = decode_json_object(json.loads(log.read_text(encoding="utf-8").splitlines()[-1])) if log.exists() else {}
    if last.get("kind") == "source_expired" and last.get("source_ref") == capture.identity:
        return Step(context="The browser document changed. Observe the page again before editing.")
    code = capture.result.call.args.get("code")
    value = capture.result.canonical.get("value")
    data = value.get("value") if isinstance(value, dict) and value.get("truncated") is False else None
    if not isinstance(data, list) or code not in (ANONYMOUS, probe.STRUCTURED):
        return Step(context="The previous capture did not contain a complete supported projection.")
    entries = [item for item in data if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]]
    counts = Counter(item["id"] for item in entries)
    ids = tuple(str(key) for key, count in counts.items() if count == 1)
    if not ids:
        return Step(context="No unique observed source is available.")
    preview = [{**{key: entry[key] for key in ("id", "title", "headers") if key in entry},
                "preview_rows": rows[:3] if isinstance(rows := entry.get("rows"), list) else []}
               for entry in entries[:8]] if code == ANONYMOUS else [{"id": entry["id"]} for entry in entries[:8]]
    context = "Observed sources: " + json.dumps(preview, ensure_ascii=False)
    if last.get("kind") == "selector":
        context += "\nCorrect the source selector using observed candidates: " + json.dumps(last["candidates"], ensure_ascii=False)
    elif conflict:
        context += "\nFile version conflict. Keep the source selection; edit will reread the file."
    return Step("table" if code == ANONYMOUS else "json", ids, context, capture)


def offered_tool(grant: Grant, step: Step) -> ProviderToolDefinition:
    if step.phase == "observe":
        schema = object_schema({"url": {"type": "string", "enum": [grant.url]}}, ["url"])
        return ProviderToolDefinition("observe", "Observe the authorized page. Runtime identifies its source format.", schema)
    if step.phase == "done":
        return ProviderToolDefinition("done", "The edit succeeded. Report it.", previous.DONE)
    fields = (["key_column", "key", "column"] if step.phase == "table" else ["pointer"])
    properties = dict.fromkeys(fields, previous.STRING)
    source_props: JsonObject = {**properties}
    if len(step.ids) > 1:
        ids: list[JsonValue] = list(step.ids)
        source_props["id"] = {"type": "string", "enum": ids}
        fields.append("id")
    target = object_schema({"file": {"type": "string", "enum": [grant.file]},
                            "field": {"type": "string", "enum": [grant.field]}}, ["file", "field"])
    schema = object_schema({"source": object_schema(source_props, fields), "target": target}, ["source", "target"])
    return ProviderToolDefinition("edit",
        f"Select source keys {fields}; use /a/b for a JSON pointer. "
        f"Set target to file {grant.file}, field {grant.field}. Runtime enforces {grant.value_type}.", schema)


def lower(action: JsonObject, grant: Grant, step: Step) -> JsonObject:
    validate_output("action", object_schema({"tool": previous.STRING, "args": {"type": "object"}}, ["tool", "args"]), action)
    offered = offered_tool(grant, step)
    if action["tool"] != offered.name:
        raise ValueError(f"Only {offered.name} is available now")
    args = decode_json_object(action["args"])
    validate_output(offered.name, offered.parameters, args)
    if offered.name == "observe":
        return {"tool": "codemode", "args": {"code": OBSERVE_CODE, "inputs": {"url": grant.url}}}
    if offered.name == "edit":
        selector = decode_json_object(args["source"])
        if len(step.ids) == 1:
            selector["id"] = step.ids[0]
        selector["type"] = grant.value_type
        return {"tool": "codemode", "args": {"code": previous.EDIT_CODE,
            "inputs": {"file": grant.file, "field": grant.field, "selector": selector}}}
    return action


class RecoveryProvider(ExperimentProvider):
    """Reuse the recorder, keeping authorization independent of the oracle."""
    def __init__(self, target: Target, directory: Path, task: str) -> None:
        super().__init__(target, directory, fixed_acquisition=False)
        self.grant = Grant.from_task(task)
        self.step = Step()

    def configure_experiment(self, payload: dict[str, object]) -> None:
        super().configure_experiment(payload)
        self.step = next_step(self.directory, self.grant)
        tool = offered_tool(self.grant, self.step)
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "authorized_browser_step", "strict": True, "schema": text_tool_schema([tool])}}
        if isinstance(messages := payload.get("messages"), list):
            messages.append({"role": "user", "content": f"Only {tool.name} is available. {tool.description}\n{self.step.context}"})

    def normalize_reply(self, reply: str) -> object:
        normalized = super().normalize_reply(reply)
        if not isinstance(normalized, str):
            return normalized
        original: JsonObject = {}
        error = ""
        try:
            original = decode_json_object(decode_answer(normalized))
            action = lower(original, self.grant, self.step)
            if original["tool"] == "edit" and self.step.capture is not None:
                inputs = decode_json_object(decode_json_object(action["args"])["inputs"])
                probe.select_value(self.step.capture.result, decode_json_object(inputs["selector"]),
                                   self.grant.url, table_projection=ANONYMOUS)
        except (ValueError, TypeError, KeyError) as exc:
            error = str(exc)
            if isinstance(exc, probe.TableSelectionError):
                append_json(self.directory / "bindings.jsonl", {"status": "rejected", "reason": error,
                            "kind": "selector", "candidates": exc.candidates})
            action = {"tool": "codemode", "args": {"code": "throw Error(inputs.reason);", "inputs": {"reason": error}}}
        append_json(self.directory / "recovery-actions.jsonl", {"original": original, "lowered": action,
                    "phase": self.step.phase, "context": self.step.context, "error": error})
        return json.dumps(action, ensure_ascii=False)


def run_arm(directory: Path, case: probe.Case, arm: str, target: Target, *, max_turns: int,
            expire: bool = False) -> JsonObject:
    task = case.task + "\n保留其他字段和 backup.json。不要运行测试或命令。先观察网页，再写入文件。"
    grant = Grant.from_task(task)
    if grant.url != case.url or grant.file != case.path:
        raise ValueError("Fixture and task scope differ")
    def factory(provider: Target, root: Path) -> ExperimentProvider:
        if arm == "B":
            return RecoveryProvider(provider, root, task)
        return previous.StructuredProvider(provider, root, case)

    return probe.run_case(directory, case, target, max_turns=max_turns, task_override=task,
        provider_factory=factory, initial_fields={grant.field: None}, expire_source=expire,
        table_projection=ANONYMOUS if arm == "B" else probe.TABLE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--max-turns", default=8, type=int)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args()
    target = select_target(90, args.base_url)
    root = args.run_dir.resolve()
    root.mkdir(parents=True)
    paths = [Path(__file__), Path(probe.__file__), Path(previous.__file__)]
    hashes: list[JsonValue] = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    source = production_source_digest()
    runs: list[JsonValue] = []
    for name, order in (("anonymous", "AB"), ("typed_json", "BA"), ("expired", "AB")):
        for arm in order:
            print(f"Starting {name}/{arm}", flush=True)
            row = run_arm(root / f"{name}-{arm}", CASES[name], arm, target, max_turns=args.max_turns, expire=name == "expired")
            runs.append({**row, "case": name, "arm": arm})
            print(f"{name}/{arm}: passed={row['passed']} stop={row['stop_reason']} calls={row['model_calls']}", flush=True)
            write_json(root / "progress.json", {"runs": runs})
    stable = source == production_source_digest()
    scripts_stable = hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    write_json(root / "summary.json", {"runs": runs, "source": source, "source_stable": stable, "scripts": hashes,
        "scripts_stable": scripts_stable, "model": target.model, "seed": target.seed, "temperature": 0})
    return 0 if stable and scripts_stable and all(isinstance(row, dict) and row["passed"] is True for row in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
