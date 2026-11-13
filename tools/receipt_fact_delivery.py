"""Experimental fact selection over committed observations; not a production tool.

The model declares needs, then selects a record collection and direct fields.
Only this module reads values. It never accepts expected answers or copied values.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from codey.policies.tool_requirement import json_pointer
from codey.types.json_value import JsonObject, JsonPrimitive, JsonValue, decode_json_object, require_json_string
from tools.completion_artifact_audit import EffectReceipt

# A fixed, field-independent DOM projection. Arbitrary model-authored evaluations
# are not accepted as extraction receipts. Only visible tables / JSON are read.
COLLECT = """(() => {
const visible = e => e.getClientRects().length > 0;
const tables = [...document.querySelectorAll('table')].filter(visible).map(t => {
  const rows = [...t.rows].filter(visible);
  const headers = [...rows[0].cells].map(c => c.innerText);
  if (new Set(headers).size !== headers.length) throw Error('duplicate column names');
  return {name:t.caption?.innerText || '', rows:rows.slice(1).map(r => {
    if (r.cells.length !== headers.length) throw Error('irregular row');
    return Object.fromEntries(headers.map((h,i) => [h,r.cells[i].innerText]));
  })};
});
const documents = [...document.querySelectorAll('pre[data-json]')].filter(visible)
  .map(e => JSON.parse(e.innerText));
return {data:{tables,documents}};
})()"""


@dataclass(frozen=True)
class Need:
    key: str
    label: str
    subject: str


@dataclass(frozen=True)
class Evidence:
    alias: str
    receipt: str
    read_receipt: str
    url: str
    document: JsonObject
    data: JsonObject
    text: str

    def collections(self) -> dict[str, list[JsonObject]]:
        """Expose whole record sets, never a model-selected individual row."""
        result: dict[str, list[JsonObject]] = {}
        tables = self.data.get("tables")
        if isinstance(tables, list):
            for index, table in enumerate(tables):
                rows = decode_json_object(table).get("rows")
                if isinstance(rows, list):
                    result[f"/tables/{index}/rows"] = [decode_json_object(row) for row in rows]
        documents = self.data.get("documents")
        if isinstance(documents, list):
            for index, document in enumerate(documents):
                _record_sets(document, f"/documents/{index}", result, root=True)
        return result

    def payload(self) -> JsonObject:
        return {"evidence": self.alias, "receipt_ref": self.receipt, "read_ref": self.read_receipt,
                "url": self.url, "data": self.data, "record_sets": list(self.collections())}


def _record_sets(value: JsonValue, path: str, result: dict[str, list[JsonObject]], *, root: bool = False) -> None:
    if isinstance(value, list):
        if all(isinstance(row, dict) for row in value):
            result[path] = [decode_json_object(row) for row in value]
        # Do not expose individual rows or nested peers within a record.
    elif isinstance(value, dict):
        if root:
            result[path] = [value]
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                _record_sets(child, path + "/" + key.replace("~", "~0").replace("/", "~1"), result)


def evidence_from_receipts(receipts: list[EffectReceipt]) -> dict[str, Evidence]:
    """Require successful parent, complete read, fixed projection and same loader."""
    parents: set[str] = set()
    current: JsonObject | None = None
    for receipt in receipts:
        result = receipt.result
        if result.call.name == "codemode" and result.ok:
            children = decode_json_object(result.canonical).get("child_effect_ids")
            if isinstance(children, list):
                parents.update(ref for ref in children if isinstance(ref, str))
        document = result.audit.get("browser_document")
        if result.call.name in {"browser_navigate", "browser_tab_new", "browser_tab_switch", "browser_tab_close"}:
            current = None
        if isinstance(document, dict):
            current = decode_json_object(document)
    result_set: dict[str, Evidence] = {}
    reads: dict[str, EffectReceipt] = {}
    for receipt in receipts:
        result = receipt.result
        if not result.ok or result.truncated or receipt.identity not in parents:
            continue
        document = result.audit.get("browser_document")
        if not isinstance(document, dict) or current != document:
            continue
        url = require_json_string(document.get("url"), "document URL")
        if result.call.name == "browser_read":
            observation = decode_json_object(result.canonical.get("research_observation"))
            source = decode_json_object(observation.get("source"))
            if source.get("truncated") is False and source.get("final_url") == url:
                reads[url] = receipt
        elif result.call.name == "browser_eval" and result.call.args.get("code") == COLLECT:
            value = decode_json_object(result.canonical.get("value"))
            if value.get("truncated") is not False or url not in reads:
                continue
            read = reads[url]
            observation = decode_json_object(read.result.canonical["research_observation"])
            alias = f"e{len(result_set) + 1}"
            result_set[alias] = Evidence(alias, receipt.identity, read.identity, url, decode_json_object(document),
                decode_json_object(decode_json_object(value["value"])["data"]),
                require_json_string(observation["text"], "page text"))
    return result_set


def declare_needs(reply: JsonObject) -> tuple[Need, ...]:
    if set(reply) != {"needs"} or not isinstance(rows := reply["needs"], list) or not rows:
        raise ValueError("Declare nonempty needs before selecting evidence.")
    needs: list[Need] = []
    for row in rows:
        item = decode_json_object(row)
        if set(item) != {"key", "label", "subject"}:
            raise ValueError("Each need requires key, label and subject.")
        values = [require_json_string(item[name], name) for name in ("key", "label", "subject")]
        if any(not value.strip() for value in values):
            raise ValueError("Needs require nonempty identifiers.")
        need = Need(*values)
        if any(existing.key == need.key for existing in needs):
            raise ValueError("Duplicate need key.")
        needs.append(need)
    return tuple(needs)


def _direct_field(row: JsonObject, pointer: str) -> JsonValue:
    if not pointer.startswith("/") or "/" in pointer[1:]:
        raise ValueError("Select one direct column / field within the matched record.")
    return json_pointer(row, pointer)


def _select(need: Need, binding: JsonObject, evidence: Mapping[str, Evidence]) -> tuple[JsonPrimitive, JsonObject]:
    if set(binding) != {"fact", "evidence", "records", "identity", "value"}:
        raise ValueError("Select positions only; copied values or extra filters are not accepted.")
    alias = require_json_string(binding["evidence"], "evidence")
    source = evidence.get(alias)
    if source is None:
        raise ValueError("Evidence is unavailable, incomplete or from a different document.")
    records = require_json_string(binding["records"], "records")
    collection = source.collections().get(records)
    if collection is None:
        raise ValueError("Select a whole observed record set, not an individual row or global value.")
    identity = require_json_string(binding["identity"], "identity")
    matched: list[tuple[int, JsonObject]] = []
    for index, row in enumerate(collection):
        try:
            if _direct_field(row, identity) == need.subject:
                matched.append((index, row))
        except (KeyError, ValueError):
            continue
    if len(matched) != 1:
        raise ValueError(f"Expected one record owned by {need.subject}; found {len(matched)}.")
    index, row = matched[0]
    pointer = require_json_string(binding["value"], "value")
    value = _direct_field(row, pointer)
    if isinstance(value, (list, dict)) or value is None or isinstance(value, str) and not value.strip():
        raise ValueError("The matched record lacks a nonempty scalar value.")
    visible = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if visible not in source.text:
        raise ValueError("The selected value was not present in the complete browser read.")
    return value, {"receipt_ref": source.receipt, "read_ref": source.read_receipt, "url": source.url,
                   "record": records + f"/{index}", "identity": identity, "value": pointer}


def deliver(needs: tuple[Need, ...], reply: JsonObject, evidence: Mapping[str, Evidence]) -> JsonObject:
    """Freeze needs across retries; partial values survive while missing facts block completion."""
    if set(reply) != {"explanation", "bindings"}:
        raise ValueError("Reply requires explanation and bindings; needs cannot be redefined.")
    explanation = require_json_string(reply["explanation"], "explanation")
    raw = reply["bindings"]
    if not isinstance(raw, list):
        raise ValueError("bindings must be a list")
    selected: dict[str, list[JsonObject]] = {}
    for row in raw:
        binding = decode_json_object(row)
        key = require_json_string(binding.get("fact"), "fact")
        if key not in {need.key for need in needs}:
            raise ValueError("Binding targets an undeclared fact.")
        selected.setdefault(key, []).append(binding)
    facts: JsonObject = {}
    proofs: JsonObject = {}
    missing: list[JsonValue] = []
    lines = [explanation]
    for need in needs:
        choices = selected.get(need.key, [])
        try:
            if len(choices) != 1:
                raise ValueError("Select exactly one evidence position for this fact.")
            value, proof = _select(need, choices[0], evidence)
        except (KeyError, ValueError) as exc:
            missing.append({"fact": need.key, "subject": need.subject, "label": need.label, "reason": str(exc)})
            lines.append(f"缺少 {need.subject} 的{need.label}：{exc}")
            continue
        facts[need.key], proofs[need.key] = value, proof
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{need.subject} 的{need.label}：{rendered}")
    return {"complete": not missing, "facts": facts, "provenance": proofs,
            "missing": missing, "text": "\n".join(lines)}
