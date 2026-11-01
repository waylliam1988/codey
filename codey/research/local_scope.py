"""Local research contracts derived from the original plan and settled receipts.

No cursor, scheduler, answer store or execution loop lives here. The ordinary
kernel freezes a scope and executes one domain action using these pure values.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from codey.knowledge.model import canonical_json
from codey.research.task_requirements import coverage_requirements, task_requirements


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def question_details(raw: Any, questions: tuple[str, ...], task: str) -> list[dict[str, Any]]:
    """Assign requirements once within the original plan, without inferring meaning."""
    if not isinstance(raw, list) or len(raw) != len(questions):
        raise ValueError("question_details needs one task_refs record per question, in the same order")
    original = {row["task_ref"] for row in task_requirements(task)} if task.strip() else set()
    expected = {row["task_ref"] for row in coverage_requirements(task)} if task.strip() else set()
    observed: set[str] = set()
    result = []
    for row in raw:
        if not isinstance(row, dict) or set(row) not in ({"task_refs"}, {"task_refs", "work_units"}):
            raise ValueError("question_details records require task_refs and optional work_units")
        refs = row["task_refs"]
        if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
                or len(set(refs)) != len(refs) or not set(refs) <= original):
            raise ValueError("question_details.task_refs must select original rN requirements")
        observed.update(refs)
        detail = {"task_refs": list(refs)}
        if "work_units" in row:
            units = row["work_units"]
            if (not isinstance(units, list) or not 1 <= len(units) <= 4
                    or any(not isinstance(text, str) or not 1 <= len(text.strip()) <= 800 for text in units)
                    or len(set(units)) != len(units)):
                raise ValueError("work_units requires 1..4 distinct complete relationship targets, up to 800 characters each")
            detail["work_units"] = list(units)
        result.append(detail)
    if not expected <= observed:
        raise ValueError("question_details must assign every substantive original requirement; missing: "
                         + ", ".join(sorted(expected - observed)))
    return result


def registered_plan(session: Any) -> dict[str, Any] | None:
    """Old frozen plans retain their old completion contract on replay."""
    plan = None
    for result in getattr(session, "_memory_results", {}).values():
        if result.ok and result.call.name == "research_plan":
            observation = result.canonical.get("research_observation", {})
            if "question_details" in observation:
                plan = {"questions": observation["questions"], "question_details": observation["question_details"]}
    return plan


def plan_units(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"question_ref": f"q{index}", "unit_ref": f"q{index}.u{unit_index}", "target": target,
             "question": text, "task_refs": detail["task_refs"]}
            for index, (text, detail) in enumerate(zip(plan["questions"], plan["question_details"], strict=True), 1)
            for unit_index, target in enumerate(detail.get("work_units", [text]), 1)]


def effective_answers(session: Any, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    units = {row["unit_ref"]: row for row in plan_units(plan)}
    selected: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for result in getattr(session, "_memory_results", {}).values():
        if not result.ok or result.call.name != "submit_research_answer":
            continue
        answer = result.canonical.get("research_answer")
        fields = {"plan_digest", "question_ref", "unit_ref", "state", "findings", "replaces", "submission_id"}
        if not isinstance(answer, dict) or set(answer) != fields:
            raise ValueError("local submission has no canonical selection")
        refs = answer["findings"]
        if (answer["state"] not in {"answered", "partial", "unresolved"}
                or not isinstance(refs, list) or len(refs) > 16
                or bool(refs) != (answer["state"] in {"answered", "partial"})
                or any(not isinstance(row, dict) or set(row) != {"ref", "object_id", "revision"}
                       or not isinstance(row["ref"], str) or re.fullmatch(r"f[1-9][0-9]*", row["ref"]) is None
                       or not isinstance(row["object_id"], str) or not row["object_id"]
                       or type(row["revision"]) is not int or row["revision"] < 1 for row in refs)
                or len({row["ref"] for row in refs}) != len(refs)):
            raise ValueError("invalid local selection state or exact references")
        body = {key: value for key, value in answer.items() if key != "submission_id"}
        identity = answer.get("submission_id")
        unit = units.get(answer.get("unit_ref", ""))
        if (identity != digest(body) or answer.get("plan_digest") != digest(plan) or unit is None
                or answer.get("question_ref") != unit["question_ref"]):
            raise ValueError("local selection differs from original plan or receipt digest")
        if identity in seen:
            continue
        previous = selected.get(unit["unit_ref"])
        if answer.get("replaces", "") != (previous["submission_id"] if previous else ""):
            raise ValueError("local selection has a stale replacement basis")
        seen.add(identity)
        selected[unit["unit_ref"]] = answer
    return selected


@dataclass(frozen=True)
class ResearchScope:
    schema_version: int
    plan_digest: str
    question_ref: str
    unit_ref: str
    question: str
    target: str
    task_refs: tuple[str, ...]
    passage_refs: tuple[str, ...]
    finding_versions: tuple[tuple[str, str, int], ...]
    replaces: str = ""
    repair_issue_json: str = ""

    @property
    def finding_refs(self) -> tuple[str, ...]:
        return tuple(row[0] for row in self.finding_versions)

    def to_payload(self) -> dict[str, Any]:
        payload = json.loads(canonical_json(asdict(self)))
        if self.schema_version == 1:
            payload.pop("repair_issue_json")
        return payload

    @classmethod
    def from_payload(cls, raw: Any) -> ResearchScope:
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] not in (1, 2):
            raise ValueError("invalid frozen research scope")
        expected = set(cls.__dataclass_fields__) - ({"repair_issue_json"} if raw["schema_version"] == 1 else set())
        if set(raw) != expected:
            raise ValueError("invalid frozen research scope")
        raw = {"repair_issue_json": "", **raw}
        if (any(not isinstance(raw[key], str) for key in
                       ("plan_digest", "question_ref", "unit_ref", "question", "target", "replaces"))
                or not isinstance(raw["repair_issue_json"], str)
                or any(not isinstance(raw[key], list) or any(not isinstance(v, str) for v in raw[key])
                       for key in ("task_refs", "passage_refs"))
                or not isinstance(raw["finding_versions"], list)):
            raise ValueError("invalid frozen research scope values")
        if raw["repair_issue_json"]:
            issue = json.loads(raw["repair_issue_json"])
            if (not raw["replaces"] or not isinstance(issue, dict)
                    or set(issue) != {"code", "path", "message", "allowed_refs", "requires_acquisition", "allowed_actions"}
                    or any(not isinstance(issue[key], str) for key in ("code", "path", "message"))
                    or type(issue["requires_acquisition"]) is not bool
                    or any(not isinstance(issue[key], list) or any(not isinstance(item, str) for item in issue[key])
                           for key in ("allowed_refs", "allowed_actions"))):
                raise ValueError("invalid frozen research repair issue")
        if (re.fullmatch(r"q[1-8]", raw["question_ref"]) is None
                or re.fullmatch(re.escape(raw["question_ref"]) + r"\.u[1-9][0-9]*", raw["unit_ref"]) is None
                or re.fullmatch(r"[0-9a-f]{64}", raw["plan_digest"]) is None
                or raw["replaces"] and re.fullmatch(r"[0-9a-f]{64}", raw["replaces"]) is None
                or any(len(set(raw[key])) != len(raw[key]) for key in ("task_refs", "passage_refs"))):
            raise ValueError("invalid frozen research scope identities")
        for row in raw["finding_versions"]:
            if (not isinstance(row, list) or len(row) != 3 or not all(isinstance(v, str) for v in row[:2])
                    or type(row[2]) is not int or row[2] < 1):
                raise ValueError("invalid frozen research finding version")
        return cls(**{**raw, "task_refs": tuple(raw["task_refs"]), "passage_refs": tuple(raw["passage_refs"]),
                      "finding_versions": tuple(tuple(row) for row in raw["finding_versions"])})


def current_scope(session: Any, *, repair: dict[str, Any] | ResearchScope | None = None) -> ResearchScope | None:
    plan = registered_plan(session)
    if plan is None:
        return None
    answers = effective_answers(session, plan)
    unit = None
    replaces = ""
    repair_issue_json = ""
    if repair is not None:
        restored = ResearchScope.from_payload(repair) if isinstance(repair, dict) else repair
        old = answers.get(restored.unit_ref)
        if restored.plan_digest != digest(plan) or old is None:
            raise ValueError("repair scope has no original selection")
        if old["submission_id"] == restored.replaces:
            unit = next(row for row in plan_units(plan) if row["unit_ref"] == restored.unit_ref)
            replaces = restored.replaces
            repair_issue_json = restored.repair_issue_json
    if unit is None:
        unit = next((row for row in plan_units(plan) if row["unit_ref"] not in answers), None)
    if unit is None:
        return None
    windows: list[tuple[str, list[str]]] = []
    bindings: dict[str, Any] = {}
    observed_at: dict[str, int] = {}
    unit_start = -1
    results = getattr(session, "_memory_results", {})
    for index, result in enumerate(results.values()):
        if not result.ok:
            continue
        if result.call.name == "submit_research_answer":
            unit_start = index
        refs = [row["ref"] for row in result.canonical.get("read_passages", [])]
        if refs:
            windows.append((result.call.args.get("question_ref", ""), refs))
        if result.call.name == "read_tool_result":
            recovered = results.get(result.call.args.get("result_ref"))
            if recovered is not None and recovered.ok:
                refs = [row["ref"] for row in recovered.canonical.get("read_passages", [])]
                if refs:
                    windows.append((unit["question_ref"], refs))
        for ref, binding in result.canonical.get("research_reference_bindings", {}).items():
            if ref not in bindings:
                observed_at[ref] = index
            if result.call.name == "knowledge_read" and result.call.args.get("id") in {ref, binding["object_id"], binding["note_id"]}:
                observed_at[ref] = index
            bindings[ref] = binding
    active_windows = [refs for question, refs in windows if question == unit["question_ref"]][-3:]
    passages = list(dict.fromkeys(ref for refs in active_windows for ref in refs))
    adopted = {row["ref"] for row in answers.get(unit["unit_ref"], {}).get("findings", [])} if replaces else set()
    candidates = [ref for ref, row in bindings.items()
                  if row["question_ref"] == unit["question_ref"] and observed_at[ref] > unit_start][-4:]
    versions = tuple((ref, row["object_id"], row["revision"]) for ref, row in bindings.items()
                     if ref in adopted or ref in candidates)
    return ResearchScope(2, digest(plan), unit["question_ref"], unit["unit_ref"], unit["question"],
                         unit["target"], tuple(unit["task_refs"]), tuple(dict.fromkeys(passages)), versions, replaces,
                         repair_issue_json)


def validate_scope_call(scope: ResearchScope, name: str, args: dict[str, Any]) -> None:
    from codey.research.questions import QUESTION_BOUND_TOOLS

    if name in QUESTION_BOUND_TOOLS and args.get("question_ref") != scope.question_ref:
        raise ValueError("question_ref must match the frozen local question: " + scope.question_ref)
    if name == "save_finding":
        refs = [args.get("passage_ref"), *args.get("additional_passage_refs", [])]
        if any(ref not in scope.passage_refs for ref in refs):
            raise ValueError("passage_ref is outside the frozen local evidence scope")
    if name in {"submit_research_answer", "save_inference"}:
        refs = args.get("findings" if name == "submit_research_answer" else "premise_refs", [])
        if any(ref not in scope.finding_refs for ref in refs):
            raise ValueError("finding is outside the frozen local selection scope")


def scoped_spec(spec: Any, scope: ResearchScope) -> Any:
    """The frozen schema and execution validation share the same reference set."""
    from codey.toolchain.tool_spec import freeze_spec_parameters, thaw_schema_value

    parameters = {key: thaw_schema_value(value) for key, value in spec.parameters}
    if "question_ref" in parameters:
        parameters["question_ref"]["enum"] = [scope.question_ref]
    if spec.name == "save_finding":
        parameters["passage_ref"]["enum"] = list(scope.passage_refs)
        parameters["additional_passage_refs"]["items"]["enum"] = list(scope.passage_refs)
    if spec.name in {"submit_research_answer", "save_inference"}:
        key = "findings" if spec.name == "submit_research_answer" else "premise_refs"
        if scope.finding_refs:
            parameters[key]["items"]["enum"] = list(scope.finding_refs)
        else:
            parameters[key]["maxItems"] = 0
    return freeze_spec_parameters(replace(spec, parameters=tuple(parameters.items())))


def submission(session: Any, ledger: Any, scope: ResearchScope | None, args: dict[str, Any]) -> dict[str, Any]:
    if scope is None:
        raise ValueError("submit_research_answer requires an original frozen research scope")
    actual = current_scope(session, repair=scope if scope.replaces else None)
    if actual is None or (actual.plan_digest, actual.unit_ref, actual.replaces) != (scope.plan_digest, scope.unit_ref, scope.replaces):
        raise ValueError("local submission scope is stale")
    validate_scope_call(scope, "submit_research_answer", args)
    state, refs = args.get("state"), args.get("findings")
    if (set(args) != {"state", "findings"} or state not in {"answered", "partial", "unresolved"}
            or not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
            or len(refs) > 16 or len(set(refs)) != len(refs)
            or (bool(refs) != (state in {"answered", "partial"}))):
        raise ValueError("answered/partial require findings; unresolved requires findings=[]")
    selections = []
    frozen = {row[0]: row[1:] for row in scope.finding_versions}
    for ref in refs:
        binding = ledger.references.get(ref)
        if (binding is None or binding.question_ref != scope.question_ref
                or (binding.object_id, binding.revision) != frozen[ref]):
            raise ValueError("selected finding version differs from the frozen request")
        selections.append({"ref": ref, "object_id": binding.object_id, "revision": binding.revision})
    body = {"plan_digest": scope.plan_digest, "question_ref": scope.question_ref, "unit_ref": scope.unit_ref,
            "state": state, "findings": selections, "replaces": scope.replaces}
    return {**body, "submission_id": digest(body)}


def assembled_report(session: Any, ledger: Any, *, pending_answer: dict[str, Any] | None = None) -> tuple[dict[str, Any], tuple[str, ...]]:
    plan = registered_plan(session)
    if plan is None:
        raise ValueError("no local research plan")
    answers = effective_answers(session, plan)
    if pending_answer is not None:
        answers[pending_answer["unit_ref"]] = pending_answer
    rows: dict[str, dict[str, Any]] = {}
    origins = []
    for unit in plan_units(plan):
        answer = answers.get(unit["unit_ref"])
        if answer is None:
            raise ValueError("local target has not been submitted: " + unit["unit_ref"])
        refs = []
        for selected in answer["findings"]:
            binding = ledger.references.get(selected["ref"])
            if binding is None or (binding.object_id, binding.revision) != (selected["object_id"], selected["revision"]):
                raise ValueError("local selection lost its exact knowledge version")
            refs.append(selected["ref"])
        attempts = [ref for ref, receipt in ledger.investigations.items() if receipt.get("question_ref") == unit["question_ref"]]
        state = {"answered": "evidence_found", "partial": "partial", "unresolved": (
            "unanswered_after_search" if attempts else "not_investigated")}[answer["state"]]
        previous = rows.get(unit["question_ref"])
        if previous is None:
            rows[unit["question_ref"]] = {"question_ref": unit["question_ref"], "state": state, "findings": refs,
                                          "investigations": attempts, "task_refs": unit["task_refs"]}
        else:
            selected = list(dict.fromkeys([*previous["findings"], *refs]))
            combined = "evidence_found" if previous["state"] == state == "evidence_found" else "partial" if selected else state
            rows[unit["question_ref"]] = {**previous, "state": combined, "findings": selected}
        origins.append(answer["submission_id"])
    return {"report_sections": {"coverage": list(rows.values())}}, tuple(origins)


def unresolved_targets(session: Any, *, pending_answer: dict[str, Any] | None = None) -> dict[str, tuple[str, ...]]:
    plan = registered_plan(session)
    if plan is None:
        return {}
    answers = effective_answers(session, plan)
    if pending_answer is not None:
        answers[pending_answer["unit_ref"]] = pending_answer
    gaps: dict[str, list[str]] = {}
    for unit in plan_units(plan):
        if answers.get(unit["unit_ref"], {}).get("state") != "answered":
            gaps.setdefault(unit["question_ref"], []).append(unit["target"])
    return {ref: tuple(targets) for ref, targets in gaps.items()}


def selection_budget(session: Any, ledger: Any, store: Any, *, pending_answer: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only feedback; intermediate targets never face a report minimum."""
    from codey.research.evidence_view import ResolvedReference
    from codey.research.issues import ResearchIssue
    from codey.research.references import resolve_version
    from codey.research.report_proposal import (
        bind_gap_investigations,
        bind_selected_answers,
        bind_unresolved_targets,
        parse_report_proposal,
        report_character_budget,
    )
    from codey.research.task_requirements import conclusion_characters, report_requirements

    plan = registered_plan(session)
    answers = effective_answers(session, plan)
    if pending_answer is not None:
        answers[pending_answer["unit_ref"]] = pending_answer
    pending = [unit["unit_ref"] for unit in plan_units(plan) if unit["unit_ref"] not in answers]
    if pending:
        refs = list(dict.fromkeys(row["ref"] for answer in answers.values() for row in answer["findings"]))
        return {"pending_targets": pending, "minimum_not_applied": True,
                "selected_finding_characters": sum(conclusion_characters(resolve_version(ledger, store, ref).value.content) for ref in refs),
                "report_constraints": report_requirements(session.task_text), "canonical_total_available": False}
    try:
        args, _ = assembled_report(session, ledger, pending_answer=pending_answer)
        proposal = bind_gap_investigations(parse_report_proposal(args, investigations=ledger.investigations), ledger.investigations)
        # Preview only the pinned claim text. Evidence access and source-chain
        # admission belong to the original completion gate, not local closure.
        resolved = tuple(ResolvedReference(ref, ledger.references[ref], resolve_version(ledger, store, ref), ())
                         for ref in proposal.selected_refs)
        proposal = bind_unresolved_targets(bind_selected_answers(proposal, resolved), unresolved_targets(session, pending_answer=pending_answer))
    except ResearchIssue as exc:
        return {"pending_targets": [], "canonical_total_available": False, "minimum_not_applied": True,
                "estimate_issue": exc.to_payload()}
    return {"pending_targets": [], "canonical_total_available": True, "minimum_not_applied": True,
            **report_character_budget(proposal, resolved, session.task_text)}


def repair_scope(session: Any, ledger: Any, store: Any, issue: dict[str, Any] | None) -> ResearchScope | None:
    """Domain diagnostics choose an existing unit; the control layer never parses prose."""
    from codey.research.references import resolve_version
    from codey.research.task_requirements import conclusion_characters

    plan = registered_plan(session)
    if plan is None:
        return None
    answers = effective_answers(session, plan)
    wanted = set((issue or {}).get("allowed_refs", []))
    units = plan_units(plan)
    candidates = [unit for unit in units if unit["unit_ref"] in answers
                  and (not wanted or wanted.intersection(row["ref"] for row in answers[unit["unit_ref"]]["findings"]))]
    if not candidates:
        return None
    unit = max(candidates, key=lambda unit: sum(conclusion_characters(resolve_version(ledger, store, row["ref"]).value.content)
        for row in answers[unit["unit_ref"]]["findings"]))
    basis = answers[unit["unit_ref"]]["submission_id"]
    seed = ResearchScope(2, digest(plan), unit["question_ref"], unit["unit_ref"], unit["question"], unit["target"],
                         tuple(unit["task_refs"]), (), (), basis, canonical_json(issue) if issue else "")
    return current_scope(session, repair=seed)
