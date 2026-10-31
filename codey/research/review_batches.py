"""Pure bounded assessment projections over original settled tool receipts.

No scheduler, cursor, provider, or store: the shared kernel advances tool turns.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from codey.knowledge.model import canonical_json

MAX_BATCH_UNITS = 8
_KINDS = ("claims", "paragraphs", "requirements", "investigations")


def batch_scopes(basis: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    work: list[tuple[str, dict[str, Any]]] = []
    for kind in _KINDS:
        rows = ([{"ref": ref}] for ref in basis["investigations"]) if kind == "investigations" else (
            [{**row, "units": [unit]} for unit in row["units"]] if kind in {"claims", "paragraphs"} else [row]
            for row in basis[kind])
        work.extend((kind, row) for group in rows for row in group)
    scopes: list[dict[str, Any]] = []
    for kind in _KINDS:
        selected = [row for category, row in work if category == kind]
        for start in range(0, len(selected), MAX_BATCH_UNITS):
            scope: dict[str, Any] = {key: [] for key in _KINDS}
            items = selected[start:start + MAX_BATCH_UNITS]
            if kind == "investigations":
                scope[kind] = {row["ref"]: basis[kind][row["ref"]] for row in items}
            elif kind in {"claims", "paragraphs"}:
                identity = "ref" if kind == "claims" else "path"
                grouped: dict[str, Any] = {}
                for row in items:
                    if row[identity] not in grouped:
                        grouped[row[identity]] = {**row, "units": []}
                    grouped[row[identity]]["units"].extend(row["units"])
                scope[kind] = list(grouped.values())
            else:
                scope[kind] = items
            scopes.append(scope)
    return tuple(scopes)


def merge_assessments(assessments: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {key: [] for key in _KINDS}
    seen: set[tuple[Any, ...]] = set()
    for assessment in assessments:
        for kind in _KINDS:
            identity = "path" if kind == "paragraphs" else "task_ref" if kind == "requirements" else "ref"
            for row in assessment[kind]:
                if kind in {"claims", "paragraphs"}:
                    target: dict[str, Any] | None = next(
                        (item for item in merged[kind] if item[identity] == row[identity]), None)
                    if target is None:
                        target = {identity: row[identity], "units": []}
                        merged[kind].append(target)
                    for unit in row["units"]:
                        key = kind, row[identity], unit["unit"]
                        if key in seen:
                            raise ValueError("duplicate assessment unit across batch receipts")
                        seen.add(key)
                        target["units"].append(copy.deepcopy(unit))
                else:
                    row_key = kind, row[identity]
                    if row_key in seen:
                        raise ValueError("duplicate assessment row across batch receipts")
                    seen.add(row_key)
                    merged[kind].append(copy.deepcopy(row))
    return merged


def batch_view(basis: dict[str, Any], index: int, prior: list[dict[str, Any]]) -> dict[str, Any]:
    scopes = batch_scopes(basis)
    scope = scopes[index]
    selected_refs = {ref for paragraph in scope["paragraphs"] for ref in paragraph["refs"]} - {
        row["ref"] for row in scope["claims"]}
    previous = merge_assessments(prior)["claims"]
    # Preserve coverage positions for paragraph gap paths, but never repeat
    # authored answers or unselected references inside another batch.
    coverage = [{"question_ref": row["question_ref"], "state": row["state"],
                 "investigations": [ref for ref in row["investigations"] if ref in scope["investigations"]]}
                for row in basis["coverage"]] if any(scope[key] for key in _KINDS[1:]) else []
    view = {**basis, **scope,
        "coverage": coverage,
        "investigations": scope["investigations"] or {},
        "investigation_scope": list(scope["investigations"]),
        "report_paths": [row["path"] for row in basis["paragraphs"]] if scope["requirements"] else [],
        "prior_claim_judgments": [row for row in previous if row["ref"] in selected_refs],
        "supporting_findings": [row for row in basis["claims"] if row["ref"] in selected_refs],
        "report_paragraphs": basis["paragraphs"] if scope["requirements"] else [],
        "batch_index": index, "batch_count": len(scopes)}
    view["batch_digest"] = hashlib.sha256(canonical_json(view).encode()).hexdigest()
    return view


def batch_receipts(session: Any, basis: dict[str, Any]) -> list[dict[str, Any]]:
    from codey.research.semantic_review import parse_review

    receipts: list[dict[str, Any]] = []
    prior: list[dict[str, Any]] = []
    for result in session._memory_results.values():
        if result.call.name != "submit_research_review" or not result.ok:
            continue
        receipt = dict(result.canonical)
        if len(receipts) >= len(batch_scopes(basis)):
            raise ValueError("extra assessment submission after the frozen scope was complete")
        view = batch_view(basis, len(receipts), prior)
        if (receipt.get("basis_digest") != basis["basis_digest"]
                or receipt.get("batch_digest") != view["batch_digest"]):
            raise ValueError("assessment batch differs from its frozen scope or dependencies")
        assessment = parse_review(canonical_json(result.call.args["assessment"]), view)
        if receipt.get("assessment") != assessment:
            raise ValueError("assessment receipt differs from its original submission")
        receipts.append(receipt)
        prior.append(assessment)
    return receipts


def next_batch(session: Any, basis: dict[str, Any]) -> dict[str, Any] | None:
    receipts = batch_receipts(session, basis)
    if len(receipts) == len(batch_scopes(basis)):
        return None
    return batch_view(basis, len(receipts), [row["assessment"] for row in receipts])


def review_spec_for_batch(spec: Any, batch: dict[str, Any]) -> Any:
    """Narrow the same frozen tool schema used by prompts and execution."""
    from dataclasses import replace

    from codey.research.knowledge_schema import research_assessment_schema
    from codey.toolchain.tool_spec import freeze_spec_parameters

    schema = research_assessment_schema()
    for kind in _KINDS:
        rows = batch[kind]
        array = schema["properties"][kind]
        array.update(minItems=len(rows), maxItems=len(rows))
        if not rows:
            continue
        properties = array["items"]["properties"]
        key = "path" if kind == "paragraphs" else "task_ref" if kind == "requirements" else "ref"
        properties[key] = {**properties[key], "enum": (
            list(rows) if kind == "investigations" else [row[key] for row in rows])}
        if kind in {"claims", "paragraphs"}:
            lengths = [len(row["units"]) for row in rows]
            units = properties["units"]
            units.update(minItems=min(lengths), maxItems=max(lengths))
            units["items"]["properties"]["unit"]["enum"] = sorted({
                unit["unit"] for row in rows for unit in row["units"]})
            unit_properties = units["items"]["properties"]
            if kind == "claims":
                unit_properties["quotes"]["items"]["properties"]["evidence"]["maximum"] = max(
                    len(row["evidence"]) for row in rows)
            else:
                gap_paths = {f"report_sections.coverage[{n}].answer" for n, row in enumerate(batch["coverage"])
                             if row["state"] != "evidence_found"}
                if not any(row["path"] in gap_paths or gap_paths and (
                        row["path"] == "summary" or row["path"].startswith("report_sections.counter[")) for row in rows):
                    unit_properties["basis"]["enum"] = ["findings"]
                refs = sorted({ref for row in rows for ref in row["refs"]})
                supports = unit_properties["finding_units"]
                if refs:
                    support = supports["items"]["properties"]
                    support["ref"] = {**support["ref"], "enum": refs}
                    support["unit"]["enum"] = sorted({unit["unit"]
                        for row in [*batch["claims"], *batch["supporting_findings"]] if row["ref"] in refs
                        for unit in row["units"]})
                else:
                    supports["maxItems"] = 0
        elif kind == "requirements":
            if not any(row["state"] != "evidence_found" for row in batch["coverage"]):
                properties["state"]["enum"] = ["addressed", "missing"]
            for field, values in (("question_refs", [f"q{n}" for n in range(1, len(batch["questions"]) + 1)]),
                                  ("report_paths", batch["report_paths"])):
                properties[field] = {**properties[field], "items": {"type": "string", "enum": values}}
        elif kind == "investigations":
            properties["question_ref"] = {**properties["question_ref"], "enum": sorted({
                row["question_ref"] for row in rows.values()})}
    return freeze_spec_parameters(replace(spec, parameters=(
        ("basis_digest", {"type": "string", "enum": [batch["basis_digest"]]}),
        ("assessment", schema))))
