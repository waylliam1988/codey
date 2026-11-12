"""Scoped browser-to-JSON copies, planned from committed tool receipts."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codey.policies.network import check_fetch_url
from codey.policies.tool_requirement import decode_answer, json_pointer
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string

if TYPE_CHECKING:
    from codey.runtime.core.models import ToolResult

_URL = re.compile(r"https?://[^\s`<>，。；！？、]+", re.IGNORECASE)
_FILE = r"([A-Za-z0-9_][A-Za-z0-9_.-]*\.json)"
_FIELD = r"([A-Za-z_][A-Za-z0-9_-]*)"
_TARGET = re.compile(
    rf"(?:写入|写进|写到|复制到|填入)\s*`?{_FILE}`?\s*的\s*`?{_FIELD}`?"
    rf"|\b(?:into|to)\s+`?{_FILE}`?\s*(?:的|\s+(?:field\s+)?(?:at\s+)?)[`/]?{_FIELD}`?",
    re.IGNORECASE,
)
_OTHER_WORKFLOW = re.compile(
    r"不要\s*(?:写|复制)|不(?:要|需)修改|换算|计算|四舍五入|翻译"
    r"|(?:用\s*|\buse\s+)`?observe\b"
    r"|\b(?:codemode|convert|calculate|round|translate|do not write)\b", re.I)
_NO_CHECK = re.compile(r"(?:不要|无需|不必)\s*(?:运行|执行|跑)?\s*(?:测试|验证)|\b(?:do not|don't|no need to)\s+(?:run\s+)?(?:tests?|verify)\b", re.I)
_CHECK = re.compile(r"(?:运行|执行|跑)\s*(?:测试|验证)|\b(?:pytest|verify|run\s+(?:the\s+)?(?:tests?|checks?))\b", re.I)

CAPTURE = """() => ({
tables:Array.from(document.querySelectorAll("table"),(table,index)=>({
 id:"table-"+(index+1), title:table.caption?.innerText.trim()||"",
 columns:Array.from(table.tHead?.rows[0]?.cells||[],cell=>cell.innerText.trim()),
 rows:Array.from(table.tBodies).flatMap(body=>Array.from(body.rows,row=>Array.from(row.cells,cell=>cell.innerText.trim()))),
 rectangular:Array.from(table.rows).every(row=>Array.from(row.cells).every(cell=>cell.colSpan===1&&cell.rowSpan===1))
})),
records:Array.from(document.querySelectorAll('script[type="application/json"]'),(script,index)=>({
 id:"json-"+(index+1), title:script.id, text:script.textContent
}))
})"""
OBSERVE = "unwrap(await tools.browser_navigate({url:inputs.url})); return unwrap(await tools.browser_eval({code:inputs.capture})).value;"
EDIT = (
    "const read=await tools.read_file({path:inputs.path});unwrap(read);"
    "return unwrap(await tools.edit({path:inputs.path,base_result_ref:read.result_ref,browser_source:inputs.source}));"
)
EXPIRED = "browser_copy_source_expired"


@dataclass(frozen=True)
class CopyTarget:
    url: str
    path: str
    field: str
    value_type: str


def copy_target(task: str) -> CopyTarget | None:
    """Recognize explicitly typed copies of one web value into one named field."""
    task = re.sub(r"```[\s\S]*?```", "", task)
    urls = {match.rstrip(".,;!?") for match in _URL.findall(task)}
    targets = list(_TARGET.finditer(task))
    integer = bool(re.search(r"整数|\binteger\b", task, re.I))
    string = bool(re.search(r"字符串|保留前导零|\b(?:string|preserve leading zeros)\b", task, re.I))
    if (len(urls) != 1 or len(targets) != 1 or integer == string or _OTHER_WORKFLOW.search(task)
            or _CHECK.search(_NO_CHECK.sub("", task))
            or not re.search(r"写入|写进|写到|复制到|填入|\b(?:copy|write)\b", task, re.I)
            or re.match(r"\s*(?:和|与|以及|及|或|、|\band\b|\bor\b)", task[targets[0].end():], re.I)):
        return None
    url = next(iter(urls))
    if check_fetch_url(url, resolve=False):
        return None
    first_path, first_field, second_path, second_field = targets[0].groups()
    return CopyTarget(url, first_path or second_path, first_field or second_field, "integer" if integer else "string")


def captured_data(receipt: ToolResult) -> JsonObject | None:
    if (receipt.call.name != "browser_eval" or receipt.call.args.get("code") != CAPTURE
            or not receipt.ok or receipt.truncated):
        return None
    value = receipt.canonical.get("value")
    if not isinstance(value, dict) or value.get("truncated") is not False:
        return None
    data = value.get("value")
    return decode_json_object(data) if isinstance(data, dict) else None


def observation_kind(data: JsonObject) -> str:
    tables, records = data.get("tables"), data.get("records")
    if isinstance(tables, list) and isinstance(records, list) and bool(tables) != bool(records):
        return "table" if tables else "json"
    return ""


def objects(data: JsonObject, kind: str) -> list[JsonObject]:
    rows = data.get("tables" if kind == "table" else "records")
    if not isinstance(rows, list):
        raise ValueError("The observation has no matching source objects.")
    return [decode_json_object(row) for row in rows]


def select_source(data: JsonObject, selector: JsonObject, value_type: str) -> JsonValue:
    kind = observation_kind(data)
    if not kind:
        raise ValueError("Observe a page with one unambiguous source format.")
    available = objects(data, kind)
    matches = [row for row in available if row.get("id") == selector.get("id")]
    if len(matches) != 1:
        raise ValueError("Choose a source ID from the current observation.")
    row = matches[0]
    if kind == "json":
        pointer = require_json_string(selector.get("pointer"), "source.pointer")
        if not pointer.startswith("/"):
            raise ValueError("Use a JSON pointer such as /shipping/number.")
        value = json_pointer(decode_answer(require_json_string(row.get("text"), "source JSON")), pointer)
    else:
        value = _table_value(row, selector)
    if value_type == "string" and isinstance(value, str):
        return value
    if value_type == "integer":
        if type(value) is int:
            return value
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,15}", value):
            return int(value)
    raise ValueError(f"The selected value must match the task's {value_type} type.")


def _table_value(table: JsonObject, selector: JsonObject) -> JsonValue:
    columns, rows = table.get("columns"), table.get("rows")
    if (table.get("rectangular") is not True or not isinstance(columns, list) or not isinstance(rows, list)
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)):
        raise ValueError("The selected table does not have a supported rectangular layout.")
    key_column, column = selector.get("key_column"), selector.get("column")
    if columns.count(key_column) != 1 or columns.count(column) != 1:
        raise ValueError(f"Choose unique columns from {json.dumps(columns, ensure_ascii=False)}.")
    index = columns.index(key_column)
    matches = [row for row in rows if isinstance(row, list) and row[index] == selector.get("key")]
    if len(matches) > 1:
        raise ValueError("The requested row key matches multiple rows. Ask which row to use; do not substitute a different key.")
    if not matches:
        keys = [row[index] for row in rows[:8] if isinstance(row, list)]
        raise ValueError(f"Row not found. Observed keys for {key_column}: {json.dumps(keys, ensure_ascii=False)}.")
    return matches[0][columns.index(column)]


def committed_children(results: Mapping[str, ToolResult]) -> set[str]:
    return {ref for result in results.values() if result.ok and result.call.name == "codemode"
            and isinstance(children := result.canonical.get("child_effect_ids"), list)
            for ref in children if isinstance(ref, str)}


@dataclass(frozen=True)
class CopyTurn:
    target: CopyTarget
    phase: str = "observe"
    ref: str = ""
    data: JsonObject | None = None
    business_key: str = ""

    def payload(self) -> JsonObject:
        return {"url": self.target.url, "path": self.target.path, "field": self.target.field, "type": self.target.value_type,
                "phase": self.phase, "ref": self.ref, "data": self.data, "business_key": self.business_key}

    @classmethod
    def restore(cls, data: JsonObject) -> CopyTurn:
        def text(key: str) -> str:
            return require_json_string(data[key], key)

        phase = text("phase")
        if phase not in {"observe", "edit", "done", "question"} or text("type") not in {"integer", "string"}:
            raise ValueError("Invalid browser copy state")
        return cls(CopyTarget(text("url"), text("path"), text("field"), text("type")), phase, text("ref"),
                   decode_json_object(data["data"]) if data["data"] is not None else None, text("business_key"))

    def spec(self, original: ToolSpec) -> ToolSpec:
        if self.phase == "observe":
            return replace(original, description="Observe the authorized webpage. The runtime detects its table or JSON data.",
                           declarations="", parameters=(), required=(), json_examples=())
        data = self.data or {}
        kind = observation_kind(data)
        available = objects(data, kind)
        source: dict[str, dict[str, object]] = {
            key: {"type": "string"} for key in (("key_column", "key", "column") if kind == "table" else ("pointer",))}
        if self.business_key and kind == "table":
            source["key"] = {"type": "string", "enum": [self.business_key]}
        if len(available) > 1:
            source["id"] = {"type": "string", "enum": [str(row["id"]) for row in available]}
        target: dict[str, dict[str, object]] = {"file": {"type": "string", "enum": [self.target.path]},
                                              "field": {"type": "string", "enum": [self.target.field]}}
        preview = [{**{key: row[key] for key in ("id", "title", "columns") if key in row},
                    **({"rows": rows[:3]} if isinstance(rows := row.get("rows"), list) else {})}
                   for row in available[:8]]
        return freeze_spec_parameters(replace(original, declarations="", json_examples=(), required=("source", "target"),
            description=f"Copy the {kind} source value to the authorized {self.target.value_type} field. "
            f"Observed source IDs and previews: {json.dumps(preview, ensure_ascii=False)}. "
            "Use a JSON Pointer such as /parcel/label for JSON. After a file version conflict, retry with the same source.",
            parameters=(("source", _object(source)), ("target", _object(target)))))

    def lower(self, args: JsonObject) -> JsonObject:
        if self.phase == "observe":
            return {"code": OBSERVE, "inputs": {"url": self.target.url, "capture": CAPTURE}}
        selector = decode_json_object(args["source"])
        data = self.data or {}
        available = objects(data, observation_kind(data))
        if len(available) == 1:
            selector["id"] = available[0]["id"]
        select_source(data, selector, self.target.value_type)
        return {"code": EDIT, "inputs": {"path": self.target.path,
            "source": {"ref": self.ref, "selector": selector}}}


def _object(properties: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def copy_turn(target: CopyTarget, task: str, results: Mapping[str, ToolResult]) -> CopyTurn:
    keys = set(re.findall(r"(?<![\w-])[A-Z]+-\d+(?![\w-])", _URL.sub("", task)))
    key = next(iter(keys)) if len(keys) == 1 else ""
    committed = committed_children(results)
    turn = CopyTurn(target, business_key=key)
    for ref, result in results.items():
        if ref in committed and (data := captured_data(result)) is not None:
            turn = CopyTurn(target, "edit" if observation_kind(data) else "question", ref, data, key)
        if result.call.name != "edit" or result.call.args.get("path") != target.path:
            continue
        if result.ok and "browser_source" in result.call.args:
            return CopyTurn(target, "done")
        if result.audit.get("error_code") == EXPIRED:
            turn = CopyTurn(target, business_key=key)
    return turn
