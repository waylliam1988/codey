"""Scoped browser-to-JSON copies, planned from committed tool receipts."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codey.codemode.json_edit import JsonNumber, json_source_value
from codey.policies.network import check_fetch_url
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object, require_json_string

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
_NO_CHECK = re.compile(r"(?:不要|无需|不必|不)\s*(?:运行|执行|跑)?\s*(?:测试|验证)|\b(?:do not|don't|no need to)\s+(?:run\s+)?(?:tests?|verify)\b", re.I)
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
    additional_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (len({field for field, _ in self.fields}) != len(self.fields)
                or any(value_type not in {"integer", "number", "string"} for _, value_type in self.fields)):
            raise ValueError("copy fields must be unique and explicitly typed")

    @property
    def fields(self) -> tuple[tuple[str, str], ...]:
        return ((self.field, self.value_type), *self.additional_fields)


def copy_target(task: str) -> CopyTarget | None:
    """Recognize explicit typed field copies to one file, without guessing types."""
    task = re.sub(r"```[\s\S]*?```|~~~[\s\S]*?~~~", "", task)
    urls = {match.group(0).rstrip(".,;!?") for match in _URL.finditer(task)}
    targets = list(_TARGET.finditer(task))
    if (len(urls) != 1 or not targets or _OTHER_WORKFLOW.search(task)
            or _CHECK.search(_NO_CHECK.sub("", task))
            or not re.search(r"写入|写进|写到|复制到|填入|\b(?:copy|write)\b", task, re.I)
            or any(re.match(r"\s*(?:和|与|以及|及|或|、|\band\b|\bor\b)", task[target.end():], re.I)
                   for target in targets)):
        return None
    url = next(iter(urls))
    if check_fetch_url(url, resolve=False):
        return None
    fields: list[tuple[str, str]] = []
    paths: set[str] = set()
    for target in targets:
        start = max(task.rfind(mark, 0, target.start()) for mark in ("。", "；", ";", "\n")) + 1
        ends = [index for mark in ("。", "；", ";", "\n") if (index := task.find(mark, target.end())) >= 0]
        clause = task[start:min(ends) if ends else len(task)]
        types = [name for name, pattern in (
            ("integer", r"整数|\binteger\b"),
            ("number", r"小数|十进制|\b(?:number|decimal|numeric)\b"),
            ("string", r"字符串|保留前导零|\b(?:string|preserve leading zeros)\b"),
        ) if re.search(pattern, clause, re.I)]
        if len(types) != 1:
            return None
        first_path, first_field, second_path, second_field = target.groups()
        paths.add(first_path or second_path)
        fields.append((first_field or second_field, types[0]))
    if len(paths) != 1 or len({field for field, _ in fields}) != len(fields):
        return None
    return CopyTarget(url, next(iter(paths)), *fields[0], tuple(fields[1:]))


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


def select_source(data: JsonObject, selector: JsonObject, value_type: str) -> JsonValue | JsonNumber:
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
        value = json_source_value(require_json_string(row.get("text"), "source JSON"), pointer)
    else:
        value = _table_value(row, selector)
    if value_type == "string" and isinstance(value, str):
        return value
    if value_type == "integer":
        text = value.text if isinstance(value, JsonNumber) else value if isinstance(value, str) else ""
        if re.fullmatch(r"-?[0-9]{1,1024}", text):
            return int(text)
    if value_type == "number":
        if isinstance(value, JsonNumber):
            return value
        if isinstance(value, str):
            return JsonNumber(value)
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
    """Parent failure does not undo children; require a complete owned sequence."""
    committed: set[str] = set()
    for parent in results.values():
        execution = parent.audit.get("codemode_execution_id")
        refs = parent.canonical.get("child_effect_ids")
        if (parent.call.name != "codemode" or not isinstance(execution, str) or not execution
                or not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)):
            continue
        children = [results[ref] for ref in refs if isinstance(ref, str) and ref in results]
        references = {ref for ref in refs if isinstance(ref, str)}
        owned = {ref for ref, child in results.items() if child.call.call_id.startswith(f"{execution}:child:")}
        if (len(children) != len(refs) or len(references) != len(refs)
                or owned != references
                or any(child.call.call_id != f"{execution}:child:{index}" for index, child in enumerate(children))):
            continue
        committed.update(references)
    return committed


def write_unconfirmed(result: ToolResult) -> bool:
    return result.call.name == "edit" and not result.ok and (
        result.canonical.get("mutation_applied") is True or result.audit.get("mutation_applied") is True)


@dataclass(frozen=True)
class CopyTurn:
    target: CopyTarget
    phase: str = "observe"
    ref: str = ""
    data_json: str = ""
    business_key: str = ""

    @property
    def data(self) -> JsonObject | None:
        """A detached view; the request owns one immutable observation."""
        return parse_json_object(self.data_json) if self.data_json else None

    def payload(self) -> JsonObject:
        target: JsonObject = ({"fields": [{"field": field, "type": kind} for field, kind in self.target.fields]}
                              if self.target.additional_fields else {"field": self.target.field, "type": self.target.value_type})
        return {"url": self.target.url, "path": self.target.path, **target,
                "phase": self.phase, "ref": self.ref, "data": self.data, "business_key": self.business_key}

    @classmethod
    def restore(cls, data: JsonObject) -> CopyTurn:
        def text(key: str) -> str:
            return require_json_string(data[key], key)

        phase = text("phase")
        if phase not in {"observe", "edit", "done", "question"}:
            raise ValueError("Invalid browser copy state")
        if "fields" in data:
            if "field" in data or "type" in data:
                raise ValueError("copy fields must have one representation")
            raw = data["fields"]
            if not isinstance(raw, list) or len(raw) < 2:
                raise ValueError("multi-field copy requires at least two fields")
            fields = [(require_json_string(row.get("field"), "field"), require_json_string(row.get("type"), "type"))
                      for item in raw for row in [decode_json_object(item)]]
            target = CopyTarget(text("url"), text("path"), *fields[0], tuple(fields[1:]))
        else:
            target = CopyTarget(text("url"), text("path"), text("field"), text("type"))
        return cls(target, phase, text("ref"),
                   json.dumps(decode_json_object(data["data"]), ensure_ascii=False, sort_keys=True)
                   if data["data"] is not None else "", text("business_key"))

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
            del source["key"]
        if len(available) > 1:
            source["id"] = {"type": "string", "enum": [str(row["id"]) for row in available]}
        preview = [{**{key: row[key] for key in ("id", "title", "columns") if key in row},
                    **({"rows": rows[:3]} if isinstance(rows := row.get("rows"), list) else {})}
                   for row in available[:8]]
        bound_key = (f"Bound row key: {json.dumps(self.business_key, ensure_ascii=False)}. "
                     if self.business_key and kind == "table" else "")
        destination: JsonObject = {"file": self.target.path, "field": self.target.field}
        if self.target.additional_fields:
            destination = {"file": self.target.path,
                           "fields": [{"field": field, "type": kind} for field, kind in self.target.fields]}
        parameter = "sources" if self.target.additional_fields else "source"
        source_schema = (_object({field: _object(source) for field, _ in self.target.fields})
                         if self.target.additional_fields else _object(source))
        return freeze_spec_parameters(replace(original, declarations="", json_examples=(), required=(parameter,),
            description=f"Copy the {kind} source value to the authorized {self.target.value_type} field. "
            f"Bound destination: {json.dumps(destination, ensure_ascii=False)}. "
            f"{bound_key}Observed source IDs and previews: {json.dumps(preview, ensure_ascii=False)}. "
            "Use a JSON Pointer such as /parcel/label for JSON. After a file version conflict, retry with the same source.",
            parameters=((parameter, source_schema),)))

    def lower(self, args: JsonObject) -> JsonObject:
        if self.phase == "observe":
            return {"code": OBSERVE, "inputs": {"url": self.target.url, "capture": CAPTURE}}
        selectors = (decode_json_object(args["sources"]) if self.target.additional_fields else
                     {self.target.field: args["source"]})
        if set(selectors) != {field for field, _ in self.target.fields}:
            raise ValueError("select exactly the declared destination fields")
        data = self.data or {}
        kind = observation_kind(data)
        available = objects(data, kind)
        bound: JsonObject = {}
        for field, value_type in self.target.fields:
            selector = decode_json_object(selectors[field])
            if self.business_key and kind == "table":
                selector["key"] = self.business_key
            if len(available) == 1:
                selector["id"] = available[0]["id"]
            select_source(data, selector, value_type)
            bound[field] = selector
        source: JsonObject = {"ref": self.ref}
        source["selectors" if self.target.additional_fields else "selector"] = (bound if self.target.additional_fields else
                                                                               bound[self.target.field])
        return {"code": EDIT, "inputs": {"path": self.target.path,
            "source": source}}


def _object(properties: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def copy_turn(target: CopyTarget, task: str, results: Mapping[str, ToolResult]) -> CopyTurn:
    for ref, result in results.items():
        if result.call.args.get("path") == target.path and write_unconfirmed(result):
            raise ValueError(f"The file write already occurred ({ref}, {target.path}), but workspace identity "
                             "is unconfirmed. Do not repeat the write. This copy operation is blocked. Check the "
                             "current file and workspace identity before starting another operation.")
    keys = {match.group(0) for match in re.finditer(r"(?<![\w-])[A-Z]+-\d+(?![\w-])", _URL.sub("", task))}
    key = next(iter(keys)) if len(keys) == 1 else ""
    committed = committed_children(results)
    turn = CopyTurn(target, business_key=key)
    for ref, result in results.items():
        data = captured_data(result)
        capture = result.ok and result.call.name == "browser_eval" and result.call.args.get("code") == CAPTURE
        if capture and (data is None or ref not in committed):
            raise ValueError("Cannot recover the browser observation: its parent/child receipt sequence is incomplete "
                             "or belongs to another execution. The source must not be fetched again automatically.")
        if data is not None:
            turn = CopyTurn(target, "edit" if observation_kind(data) else "question", ref,
                            json.dumps(data, ensure_ascii=False, sort_keys=True), key)
        if ref not in committed or result.call.name != "edit" or result.call.args.get("path") != target.path:
            continue
        if result.ok and "browser_source" in result.call.args:
            return CopyTurn(target, "done")
        if result.audit.get("error_code") == EXPIRED:
            turn = CopyTurn(target, business_key=key)
    return turn
