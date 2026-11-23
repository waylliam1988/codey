"""Exact arithmetic over original read receipts, callable from the existing CodeMode host."""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import replace
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import TYPE_CHECKING

from codey.research.knowledge_schema import array_schema, object_schema
from codey.research.ledger_receipts import source_observation
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    decode_json_value,
    require_json_integer,
    require_json_string,
)

if TYPE_CHECKING:
    from codey.research.answer_contract import AnswerScope, AnswerSessionView
    from codey.toolchain.tool_spec import ToolSpec

OPERATIONS = ("sum", "subtract", "multiply", "divide", "percentage", "growth_rate", "percentage_points", "mean", "weighted_mean", "convert", "count")
_UNITS = {"": ("scalar", "1"), "%": ("percent", "1"), "kg": ("mass", "1000"), "g": ("mass", "1"),
    "mg": ("mass", "0.001"), "µg": ("mass", "0.000001"), "μg": ("mass", "0.000001"),
    "公斤": ("mass", "1000"), "克": ("mass", "1"), "l": ("volume", "1000"), "ml": ("volume", "1"),
    "km": ("length", "1000"), "m": ("length", "1"), "cm": ("length", "0.01"), "mm": ("length", "0.001"),
    "h": ("time", "3600"), "小时": ("time", "3600"), "min": ("time", "60"), "分钟": ("time", "60"),
    "s": ("time", "1"), "ms": ("time", "0.001"), "天": ("time", "86400"), "人": ("people", "1"),
    "°c": ("temperature", "1"), "°f": ("temperature", "1")}
_QUANTITY = re.compile(r"(?<![\w.])([+-]?\d+(?:\.\d+)?)\s*(" +
    "|".join(re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True) if unit) + r")(?![A-Za-z])", re.I)
_OPERAND = object_schema({"result_ref": {"type": "string"}, "pointer": {"type": "string", "maxLength": 2048}}, "result_ref", "pointer")
_TABLE = object_schema({"result_ref": {"type": "string"}, "value_column": {"type": "string"},
    "weight_column": {"type": "string"}, "where": object_schema({"column": {"type": "string"},
        "equals": {"type": "string"}}, "column", "equals")}, "result_ref", "value_column", "where")


def is_exact_quantity(text: str) -> bool:
    return _QUANTITY.fullmatch(text) is not None


def data_schema() -> JsonObject:
    return object_schema({"evidence": {"type": "string"}, "result_ref": {"type": "string"}, "path": {"type": "string"},
        "format": {"type": "string", "enum": ["quantities", "csv", "json"]}}, "format")


def read_data_spec(spec: ToolSpec, scope: AnswerScope | None) -> ToolSpec:
    from codey.toolchain.tool_spec import freeze_spec_parameters

    if scope is not None and scope.aliases:
        return freeze_spec_parameters(replace(spec, parameters=tuple((key,
            {"type": "string", "enum": [label for label, _ in scope.aliases]} if key == "evidence" else value)
            for key, value in spec.parameters)))
    return freeze_spec_parameters(replace(spec,
        parameters=tuple((key, value) for key, value in spec.parameters if key != "evidence"),
        description="Parse a complete original read_file receipt. Choose the already-read path or its result_ref. "
        "Read the file first. In CodeMode reuse the same path in tools.read_data after tools.read_file."))


def calculation_schema() -> JsonObject:
    return object_schema({"operation": {"type": "string", "enum": list(OPERATIONS)},
        "operands": array_schema(_OPERAND, 1, 1000), "weights": array_schema(_OPERAND, 1, 1000),
        "table": _TABLE,
        "unit": {"type": "string"}, "scale": {"type": "integer", "minimum": 0, "maximum": 12},
        "missing": {"type": "string", "enum": ["error", "skip"]}}, "operation")


def data_output_schema() -> JsonObject:
    quantity = object_schema({"value": {"type": "string"}, "unit": {"type": "string"},
        "start": {"type": "integer"}, "end": {"type": "integer"}, "context": {"type": "string"}},
        "value", "unit", "start", "end", "context")
    return {"anyOf": [object_schema({"format": {"type": "string", "enum": [name]}, "data": data,
        "source_ref": {"type": "string"}, "source_version": {"type": "string"},
        "passage_refs": {"type": "array", "items": {"type": "string"}}},
        "format", "data", "source_ref", "source_version", "passage_refs") for name, data in (
            ("quantities", {"type": "array", "items": quantity}),
            ("csv", {"type": "array", "items": {"type": "object", "additionalProperties": {"type": "string"}}}),
            ("json", JsonObject()))]}


def calculation_output_schema() -> JsonObject:
    fields: JsonObject = {key: {"type": "string"} for key in ("operation", "value", "unit", "rounding", "validation")}
    fields.update({"scale": {"type": "integer"}, "missing_skipped": {"type": "integer"},
        "inputs": {"type": "array", "items": _OPERAND},
        "passage_refs": {"type": "array", "items": {"type": "string"}}})
    return object_schema(fields, *fields)


def _pointer(value: JsonValue, pointer: str) -> JsonValue:
    if not pointer.startswith("/") or len(pointer) > 2048:
        raise ValueError("select a JSON pointer from an original result")
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list) and token.isdecimal():
            value = value[int(token)]
        else:
            raise ValueError("calculation pointer does not select a value")
    return value


def _read_file_reference(path: str, session: AnswerSessionView) -> str:
    selected = path.replace("\\", "/").removeprefix("./")
    for ref, result in reversed(tuple(session._memory_results.items())):
        if ref not in session.executed or result.call.name != "read_file" or not result.ok:
            continue
        if isinstance(observed := result.call.args.get("path"), str) and observed.replace("\\", "/").removeprefix("./") == selected:
            data = decode_json_object(result.canonical.get("value"))
            if (data.get("start_line") != 1 or data.get("next_offset") != 0
                    or data.get("end_line") != data.get("total_lines")):
                raise ValueError("read the entire selected file before parsing its data")
            return ref
    raise ValueError("read the selected file in this task before parsing its data")


def read_data(args: JsonObject, session: AnswerSessionView, scope: AnswerScope | None) -> JsonObject:
    evidence, reference, path = args.get("evidence"), args.get("result_ref"), args.get("path")
    if sum(value is not None for value in (evidence, reference, path)) != 1:
        raise ValueError("select exactly one observed evidence label, read_file result_ref or already-read path")
    passage_refs: list[JsonValue] = []
    source_ref = ""
    version = ""
    if evidence is not None:
        passage = dict(scope.aliases).get(require_json_string(evidence, "evidence")) if scope is not None else None
        if passage is None:
            raise ValueError("select an original frozen evidence label")
        text = ""
        for ref, result in session._memory_results.items():
            if ref not in session.executed or not result.ok:
                continue
            descriptors = result.canonical.get("read_passages", [])
            if not isinstance(descriptors, list):
                continue
            for item in descriptors:
                row = decode_json_object(item)
                if row.get("ref") == passage:
                    source, body, _ = source_observation(result.canonical.get("research_observation"))
                    text = body[require_json_integer(row.get("start"), "start"):require_json_integer(row.get("end"), "end")]
                    source_ref, version = ref, source.text_hash
                    passage_refs = [passage]
        if not source_ref:
            raise ValueError("original passage receipt is unavailable")
    else:
        source_ref = (_read_file_reference(require_json_string(path, "path"), session) if path is not None
                      else require_json_string(reference, "result_ref"))
        original = session._memory_results.get(source_ref)
        if source_ref not in session.executed or original is None or not original.ok or original.call.name != "read_file":
            raise ValueError("select a successful original read_file receipt")
        data = decode_json_object(original.canonical.get("value"))
        if data.get("text_complete") is not True or original.truncated:
            raise ValueError("table parsing requires a complete read_file receipt")
        text = require_json_string(data.get("text"), "read text")
        version = require_json_string(original.canonical.get("file_version"), "file version")
    if len(text) > 200000:
        raise ValueError("calculation input exceeds 200000 characters")
    format_name = args.get("format")
    parsed = _parse_data(text, format_name)
    return {"format": format_name, "data": parsed, "source_ref": source_ref, "source_version": version,
            "passage_refs": passage_refs}


def _parse_data(text: str, format_name: JsonValue) -> JsonValue:
    parsed: JsonValue
    if format_name == "quantities":
        parsed = [{"value": match[1], "unit": match[2], "start": match.start(), "end": match.end(),
            "context": text[max(text.rfind("\n", 0, match.start()) + 1, match.start() - 60):
                min(text.find("\n", match.end()) if "\n" in text[match.end():] else len(text), match.end() + 60)]}
            for match in _QUANTITY.finditer(text)]
    elif format_name == "csv":
        rows: list[JsonValue] = []
        reader = csv.reader(io.StringIO(text))
        columns = next(reader, list[str]())
        if not columns or len(set(columns)) != len(columns) or any(not name for name in columns):
            raise ValueError("CSV needs distinct nonempty column names")
        for cells in reader:
            if not cells:
                continue
            if len(cells) != len(columns) or len(rows) >= 1000:
                raise ValueError("CSV is ragged or exceeds 1000 rows")
            rows.append(dict(zip(columns, cells, strict=True)))
        parsed = rows
    elif format_name == "json":
        raw: object = json.loads(text, parse_float=str, parse_int=str)
        parsed = decode_json_value(raw)
    else:
        raise ValueError("unsupported data format")
    if isinstance(parsed, list) and len(parsed) > 1000:
        raise ValueError("data exceeds 1000 rows or quantities; select a smaller original read range")
    return parsed


def _decimal(value: JsonValue) -> Decimal:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool) or len(str(value)) > 64:
        raise ValueError("operand is not a bounded decimal")
    number = Decimal(str(value))
    if not number.is_finite() or number.adjusted() > 24 or number.adjusted() < -24:
        raise ValueError("operand is nonfinite or exceeds decimal limits")
    return number


def _operand(raw: JsonValue, session: AnswerSessionView, *, count: bool = False) -> tuple[Decimal | None, str, list[JsonValue]]:
    row = decode_json_object(raw)
    ref, pointer = require_json_string(row.get("result_ref"), "result_ref"), require_json_string(row.get("pointer"), "pointer")
    source = session._memory_results.get(ref)
    if ref not in session.executed or source is None or not source.ok or source.call.name not in {"read_data", "calculate"}:
        raise ValueError("operand must select a committed read_data or calculate receipt")
    data = decode_json_object(source.canonical.get("value"))
    if source.call.name == "read_data" and not pointer.startswith("/data/"):
        raise ValueError("select an original /data/ field, not receipt metadata")
    value = _pointer(data, pointer)
    unit = ""
    if source.call.name == "calculate":
        if pointer != "/value":
            raise ValueError("select /value from a calculation")
        unit = require_json_string(data.get("unit"), "unit")
    elif data.get("format") == "quantities":
        if not pointer.endswith("/value"):
            raise ValueError("select the original quantity's /value field")
        unit = require_json_string(decode_json_object(_pointer(data, pointer.rsplit("/", 1)[0])).get("unit"), "unit")
    passages = data.get("passage_refs", [])
    if not isinstance(passages, list):
        raise ValueError("invalid original calculation provenance")
    number = None if value in ("", None) else Decimal(1) if count else _decimal(value)
    return number, "" if count else unit, passages


def _convert(value: Decimal, source: str, target: str) -> Decimal:
    source, target = source.casefold(), target.casefold()
    if source == target:
        return value
    if {source, target} == {"°c", "°f"}:
        return value * 9 / 5 + 32 if source == "°c" else (value - 32) * 5 / 9
    first, second = _UNITS.get(source), _UNITS.get(target)
    if first is None or second is None or first[0] != second[0] or first[0] == "temperature":
        raise ValueError("units are incompatible; no implicit denominator or dimension conversion")
    return value * Decimal(first[1]) / Decimal(second[1])


def _table_operands(args: JsonObject, session: AnswerSessionView) -> JsonObject:
    if "operands" in args or "weights" in args:
        raise ValueError("choose a CSV table selection or explicit operands and weights")
    table = decode_json_object(args["table"])
    where = decode_json_object(table.get("where"))
    ref = require_json_string(table.get("result_ref"), "table.result_ref")
    value_column = require_json_string(table.get("value_column"), "table.value_column")
    weight_column = require_json_string(table.get("weight_column", ""), "table.weight_column")
    if bool(weight_column) != (args.get("operation") == "weighted_mean"):
        raise ValueError("a CSV weight column is required only for weighted_mean")
    column = require_json_string(where.get("column"), "table.where.column")
    equals = require_json_string(where.get("equals"), "table.where.equals")
    original = session._memory_results.get(ref)
    if ref not in session.executed or original is None or not original.ok or original.call.name != "read_data":
        raise ValueError("table.result_ref must select a committed CSV read_data receipt")
    parsed = decode_json_object(original.canonical.get("value"))
    rows = parsed.get("data")
    if parsed.get("format") != "csv" or not isinstance(rows, list) or not rows:
        raise ValueError("table selection requires a nonempty CSV read_data receipt")
    fields = {column, value_column, *((weight_column,) if weight_column else ())}
    selected: list[int] = []
    for index, entry in enumerate(rows):
        row = decode_json_object(entry)
        if not fields <= row.keys():
            raise ValueError("select columns from the original CSV receipt")
        if row[column] == equals:
            selected.append(index)

    def pointers(name: str) -> list[JsonValue]:
        token = name.replace("~", "~0").replace("/", "~1")
        return [{"result_ref": ref, "pointer": f"/data/{index}/{token}"} for index in selected]

    return {**args, "operands": pointers(value_column), **({"weights": pointers(weight_column)} if weight_column else {})}


def calculate(args: JsonObject, session: AnswerSessionView) -> JsonObject:
    if "table" in args:
        args = _table_operands(args, session)
    raw = args.get("operands")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 1000:
        raise ValueError("calculation needs 1..1000 referenced operands")
    operation = require_json_string(args.get("operation"), "operation")
    scale = require_json_integer(args.get("scale", 6), "scale")
    if not 0 <= scale <= 12:
        raise ValueError("scale must be 0..12")
    selected = [_operand(row, session, count=operation == "count") for row in raw]
    missing = sum(number is None for number, _, _ in selected)
    if missing and (args.get("missing", "error") != "skip" or operation == "weighted_mean"):
        raise ValueError("missing input; explicitly choose skip or filter value/weight pairs in CodeMode")
    selected = [row for row in selected if row[0] is not None]
    if not selected:
        raise ValueError("no numeric operands remain")
    unit = require_json_string(args.get("unit", selected[0][1]), "unit")
    raw_weights = args.get("weights", [])
    if not isinstance(raw_weights, list) or raw_weights and operation != "weighted_mean":
        raise ValueError("weights are only valid for weighted_mean")
    weights = [_operand(row, session) for row in raw_weights]
    passages: list[JsonValue] = []
    for _, _, refs in [*selected, *weights]:
        for ref in refs:
            if ref not in passages:
                passages.append(ref)
    with localcontext() as context:
        context.prec = 60
        numbers = [_convert(number, original, unit) for number, original, _ in selected if number is not None]
        value, unit = _operation(operation, numbers, unit, weights)
        value = value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_EVEN)
    rendered = format(value, "f").rstrip("0").rstrip(".") if scale else format(value, "f")
    return {"operation": operation, "value": rendered if value else "0", "unit": unit,
            "scale": scale, "rounding": "half_even", "missing_skipped": missing,
            "inputs": raw, "passage_refs": passages, "validation": "execution_and_arithmetic; interpretation_unreviewed"}


def _operation(operation: str, numbers: list[Decimal], unit: str,
               weights: list[tuple[Decimal | None, str, list[JsonValue]]]) -> tuple[Decimal, str]:
    total = sum(numbers, Decimal(0))
    if operation == "sum":
        return total, unit
    if operation == "mean":
        return total / len(numbers), unit
    if operation == "count":
        return Decimal(len(numbers)), "items"
    if operation == "weighted_mean":
        if len(weights) != len(numbers):
            raise ValueError("weighted mean requires one referenced weight per value")
        if any(value is None or value < 0 or dimension not in {"", "人"} for value, dimension, _ in weights):
            raise ValueError("weights must be nonnegative, present and dimensionless or counts")
        real = [value for value, _, _ in weights if value is not None]
        return sum((number * weight for number, weight in zip(numbers, real, strict=True)), Decimal(0)) / sum(real), unit
    if operation == "convert" and len(numbers) == 1:
        return numbers[0], unit
    if len(numbers) != 2:
        raise ValueError("this operation needs exactly two operands")
    first, second = numbers
    if operation == "subtract":
        return first - second, unit
    if operation == "divide":
        return first / second, ""
    if operation == "percentage":
        return first * 100 / second, "%"
    if operation == "growth_rate":
        return (second - first) * 100 / first, "%"
    if operation == "percentage_points" and unit == "%":
        return second - first, "percentage points"
    if operation == "multiply" and not unit:
        return first * second, ""
    raise ValueError("unsupported operation or unit combination")


def calculation_fields(session: AnswerSessionView) -> tuple[JsonObject, ...]:
    fields: list[JsonObject] = []
    for ref, result in session._memory_results.items():
        if ref in session.executed and result.ok and result.call.name == "calculate":
            value = decode_json_object(result.canonical.get("value"))
            fields.append({"label": f"c{len(fields) + 1}", "result_ref": ref, "value": value["value"],
                           "operation": value["operation"], "unit": value["unit"], "passage_refs": value["passage_refs"]})
    return tuple(fields)


def calculation_choices(fields: tuple[JsonObject, ...]) -> list[JsonValue]:
    return [{"slot": "{{calc:" + require_json_string(field.get("label"), "calculation label") + "}}",
             "operation": field["operation"], "value": field["value"], "unit": field["unit"]} for field in fields]


def render_calculations(markdown: str, fields: tuple[JsonObject, ...], aliases: tuple[tuple[str, str], ...]) -> str:
    by_label = {require_json_string(field.get("label"), "calculation label"): field for field in fields}
    evidence = {ref: label for label, ref in aliases}
    choices = ", ".join(str(decode_json_object(row)["slot"]) for row in calculation_choices(fields)) or "none; calculate first"

    def expand(match: re.Match[str]) -> str:
        field = by_label.get(match[1])
        if field is None:
            raise ValueError("unknown calculation slot; choose one of these exact slots: " + choices)
        refs = field["passage_refs"]
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in evidence for ref in refs):
            raise ValueError("calculation source is outside this report scope")
        return (str(field["value"]) + (" " + str(field["unit"]) if field["unit"] else "") + " " +
                "".join(f"[{evidence[str(ref)]}]" for ref in refs)).rstrip()

    rendered = re.sub(r"\{\{calc:(c[1-9][0-9]*)\}\}", expand, markdown)
    if "{{calc:" in rendered:
        raise ValueError("invalid calculation slot; choose one of these exact slots: " + choices)
    return rendered
