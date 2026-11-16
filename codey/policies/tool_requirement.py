"""Tool-success predicates encoded in the existing immutable required checks."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import NoReturn

from codey.automation.browser_document import same_document
from codey.policies.output_requirement import EXPLANATION_GUIDANCE, JSON_ANSWER_GUIDANCE
from codey.policies.requirement import RequirementEvaluation
from codey.research.source_document import SourceReadScope
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    decode_json_value,
    parse_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)

PREFIX = "tool_succeeded:"
MAX_REQUIRED_TOOLS = 16
_NAME = re.compile(r"[a-z][a-z0-9_.]{0,63}\Z")


def required_tool_names(raw: object) -> tuple[str, ...]:
    """Admit tool names or canonical predicates, never authorization grants."""
    if not isinstance(raw, (tuple, list)) or len(raw) > MAX_REQUIRED_TOOLS:
        raise ValueError(f"required_tools must be a list of at most {MAX_REQUIRED_TOOLS} canonical tool names")
    declarations: list[str] = []
    for name in raw:
        if not isinstance(name, str):
            raise ValueError("required_tools must contain canonical tool names or predicates")
        if _NAME.fullmatch(name) is None:
            from codey.policies.browser_requirement import parse_browser_requirement

            try:
                if parse_browser_requirement(name) is not None:
                    declarations.append(name)
                    continue
                requirement = parse_tool_success_check(name)
            except (ValueError, TypeError) as exc:
                raise ValueError("required_tools contains an invalid tool predicate") from exc
            if requirement is None:
                raise ValueError("required_tools must contain canonical tool names or predicates")
        declarations.append(name)
    return tuple({name: None for name in declarations})


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def argument_digest(value: object) -> str:
    """Type-preserving selectors; do not retain file bodies in the projection."""
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolObservation:
    """One ephemeral selection from the original committed receipts."""
    evaluation: RequirementEvaluation
    value: JsonValue = None
    result_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserTextSelector:
    url: str
    prefix: str
    suffix: str
    context: str = ""
    query_result: bool = False

    def __post_init__(self) -> None:
        from codey.policies.network import check_fetch_url

        if not self.url or len(self.url) > 2048 or self.url != self.url.strip() or check_fetch_url(self.url, resolve=False):
            raise ValueError("browser text selectors require an eligible, explicit page URL")
        if any(not marker.strip() or len(marker) > 256 or marker.splitlines() != [marker]
               for marker in (self.prefix, self.suffix)):
            raise ValueError("browser text selectors require bounded, nonempty single-line markers")
        if not isinstance(self.context, str) or len(self.context) > 256 or self.context != self.context.strip():
            raise ValueError("browser text context must be a bounded observed record label")
        if type(self.query_result) is not bool:
            raise ValueError("query_result must be a boolean")

    def select(self, text: str) -> str:
        start = text.find(self.prefix)
        if start < 0 or text.find(self.prefix, start + 1) >= 0:
            raise ValueError("expected one browser text prefix")
        lines = text[start + len(self.prefix):].splitlines()
        value, end, _ = lines[0].partition(self.suffix) if lines else ("", "", "")
        if not end or not value.strip() or len(value) > 1024:
            raise ValueError("expected a bounded value followed by its suffix on the same line")
        return value


@dataclass(frozen=True)
class ToolSuccessRequirement:
    tool: str
    via: str = ""
    arguments_json: str = "{}"
    answer_pointer: str | None = None
    result_pointer: str = ""
    result_line: int | None = None
    browser_text: BrowserTextSelector | None = None
    answer_contains: bool = False
    omit_from_explanation: bool = False
    after: str = ""
    stage: int = 0
    file_path: str = ""
    value_type: str = ""
    report_label: str = ""
    report_allow_missing: bool = False

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.tool) or (self.via and not _NAME.fullmatch(self.via)):
            raise ValueError("tool requirement names must be canonical tool names")
        args = parse_json_object(self.arguments_json)
        object.__setattr__(self, "arguments_json", _json(args))
        if self.after and (self.tool, self.after) not in {
                ("browser_wait", "page_actions"), ("browser_read", "browser_wait")}:
            raise ValueError("browser ordering requires wait after page actions or read after wait")
        if type(self.stage) is not int or not 0 <= self.stage <= 16 or (self.stage and not self.after):
            raise ValueError("browser stage requires an ordered wait/read and a bounded click ordinal")
        if self.answer_pointer is not None:
            _validate_pointer(self.answer_pointer)
            _validate_pointer(self.result_pointer)
        elif self.result_pointer or self.result_line is not None:
            raise ValueError("result selectors require an answer pointer")
        if type(self.answer_contains) is not bool or (self.answer_contains and self.answer_pointer != "/explanation"):
            raise ValueError("answer containment requires the /explanation string destination")
        if type(self.omit_from_explanation) is not bool or (self.omit_from_explanation and (
                self.answer_pointer in {None, "", "/explanation"} or self.answer_contains)):
            raise ValueError("explanation omission requires an independent answer field")
        if self.result_line is not None and (
            type(self.result_line) is not int or self.result_line < 1
            or self.tool != "read_file" or self.result_pointer != "/text"
        ):
            raise ValueError("absolute result lines require read_file /text and a positive integer")
        if self.browser_text is not None and (
            self.tool != "browser_read" or (self.via != "codemode" and not self.file_path and not self.report_label) or not self.answer_pointer
            or self.result_pointer != "/text" or self.result_line is not None
        ):
            raise ValueError("browser text selectors require browser_read via codemode and a named answer field")
        if (type(self.report_allow_missing) is not bool or (self.report_allow_missing and not self.report_label)
                or not isinstance(self.report_label, str) or len(self.report_label) > 256
                or (self.report_label and (not self.report_label.strip() or self.report_label.splitlines() != [self.report_label]
                    or self.browser_text is None or self.file_path or self.answer_contains or self.omit_from_explanation))):
            raise ValueError("report fields require a browser selector, label and independent Markdown destination")
        if self.file_path or self.value_type:
            from pathlib import PurePosixPath

            path = PurePosixPath(self.file_path)
            if (not self.file_path or path.is_absolute() or ".." in path.parts or ":" in self.file_path
                    or "\\" in self.file_path or path.as_posix() != self.file_path
                    or self.value_type not in {"integer", "number", "string"} or self.browser_text is None
                    or self.answer_contains or self.omit_from_explanation or not self.answer_pointer
                    or self.answer_pointer.count("/") != 1):
                raise ValueError("file field bindings require a relative path, top-level pointer and explicit source type")

    @property
    def declaration(self) -> str:
        if not self.via and self.arguments_json == "{}" and self.answer_pointer is None and not self.after:
            return PREFIX + self.tool
        data: JsonObject = {"tool": self.tool, "via": self.via,
                            "args": parse_json_object(self.arguments_json)}
        if self.after:
            data["after"] = self.after
        if self.stage:
            data["stage"] = self.stage
        if self.answer_pointer is not None:
            answer: JsonObject = {"pointer": self.answer_pointer, "result_pointer": self.result_pointer,
                                  "line": self.result_line}
            if self.browser_text is not None:
                selector: JsonObject = {"url": self.browser_text.url, "prefix": self.browser_text.prefix,
                                        "suffix": self.browser_text.suffix}
                if self.browser_text.context:
                    selector["context"] = self.browser_text.context
                if self.browser_text.query_result:
                    selector["query_result"] = True
                answer["browser_text"] = selector
            if self.answer_contains:
                answer["contains"] = True
            if self.omit_from_explanation:
                answer["omit_from_explanation"] = True
            if self.file_path:
                answer.update(file_path=self.file_path, value_type=self.value_type)
            if self.report_label:
                answer.update(report_label=self.report_label, report_allow_missing=self.report_allow_missing)
            data["answer"] = answer
        return PREFIX + _json(data)

    @property
    def check_id(self) -> str:
        if not self.via and self.arguments_json == "{}" and self.answer_pointer is None and not self.after:
            return self.declaration
        # Completion proofs contain bounded identifiers, never raw selectors.
        return PREFIX + hashlib.sha256(self.declaration.encode("utf-8")).hexdigest()[:32]

    @property
    def execution_guidance(self) -> str:
        target = self.tool + (f" with arguments matching {self.arguments_json}" if self.arguments_json != "{}" else "")
        if self.via:
            execution = (f"Required execution: inside {self.via}, call tools.{target}. "
                    "The child call and its parent must both commit successfully. "
                    f"A separate top-level {self.tool} call does not satisfy this requirement.")
        elif self.report_label:
            execution = "Required evidence: successfully read the declared source and record through a formal source-reading tool. "
        else:
            execution = (f"Required execution: call {target} and receive a successful tool result. "
                         "Displayed examples and failed calls do not satisfy this requirement.")
        if self.after:
            execution += (" Use the current document, after its latest page action." if self.after == "page_actions" else
                          " Read the current document after its latest successful declared wait.")
        if self.stage:
            execution += f" This requirement belongs to click stage {self.stage}, before the next click."
        return execution

    @property
    def guidance(self) -> str:
        return self.model_guidance()

    def model_guidance(self, *, explanation: bool = False, execution: str | None = None) -> str:
        execution = self.execution_guidance if execution is None else execution
        if self.answer_pointer is None:
            return execution
        if self.report_label:
            return (execution + f" Deliver the explicitly declared field {self.report_label!r}. "
                    f"Use {{{{field:{self.answer_pointer}}}}} in submit_research_answer.markdown; the application "
                    "expands its frozen exact value and original citation. Repair only the answer when evidence is ready.")
        if self.file_path:
            return (execution + f" File {self.file_path!r} field {self.answer_pointer!r} must match the committed "
                    f"browser text selector as declared JSON {self.value_type}. Follow the task's declared edit mode "
                    "and the next-step template. Do not infer types or coerce other fields.")
        selected = ("research_observation.text" if self._binds_browser_body
                    else f"value{self.result_pointer}")
        if self.result_line is not None:
            selected += f", absolute file line {self.result_line}"
        if self.browser_text is not None:
            selected += (f" on {self.browser_text.url}, between literal markers "
                         f"{self.browser_text.prefix!r} and {self.browser_text.suffix!r}")
        instruction = EXPLANATION_GUIDANCE if explanation else JSON_ANSWER_GUIDANCE
        if self.answer_contains:
            return (execution + " " + instruction + f" The explanation must include the complete text selected from {selected}. "
                    "Match numbers as complete JSON numbers, not as parts of larger numbers or identifiers. "
                    "The application checks this occurrence without rewriting your explanation. "
                    "Reuse committed evidence when repairing only the answer.")
        content = ("Supply only natural-language explanation text. " if explanation else
                   "In done.summary, supply only unbound JSON fields, or {} when none are needed. ")
        evidence = ("The latest matching receipt for the current browser document must be complete."
                    if self._binds_latest_browser_fields else
                    "Current document receipts must be complete and agree." if self.binds_browser_fields else
                    "Conflicting observations cannot establish this answer requirement.")
        return (execution + " " + instruction + f" The application fills pointer {self.answer_pointer!r} "
                f"from the committed {selected}, preserving its JSON type and complete value. "
                + content + evidence + (" Do not repeat this selected value in /explanation."
                                        if self.omit_from_explanation else ""))

    def feedback_reason(self, evaluation: RequirementEvaluation) -> str:
        """Keep evidence diagnostics when the request renders another input binding.

        Execution repair is appended by this rule, so remove only that exact
        suffix. Answer repairs, including their committed evidence, stay intact.
        """
        return evaluation.feedback.removesuffix(self.execution_guidance).rstrip()

    def evaluate(
        self, records: Mapping[str, ExecutionRecord], *, final_text: str = "", results: Mapping[str, ToolResult] | None = None,
        browser_document: JsonObject | None = None,
    ) -> RequirementEvaluation:
        return self.evaluate_observation(self.observe(records, results=results, browser_document=browser_document), final_text)

    def observe(
        self, records: Mapping[str, ExecutionRecord], *, results: Mapping[str, ToolResult] | None = None,
        browser_document: JsonObject | None = None,
    ) -> ToolObservation:
        expected = {key: argument_digest(value)
                    for key, value in parse_json_object(self.arguments_json).items()}
        eligible: list[tuple[str, ExecutionRecord]] = []
        observed_reads: list[tuple[str, ExecutionRecord]] = []
        witnesses: set[str] = set()
        matching_execution = False
        ordered: set[str] | None = None
        prerequisite_refs: frozenset[str] = frozenset()
        if self.after:
            ordered, prerequisite_refs = _ordered_requirement_refs(self, records, results or {}, browser_document)
        for identity, row in records.items():
            name = row.get("name")
            if not isinstance(name, str) or not _succeeded(row, name):
                continue
            if self.report_label and self._accepts_tool(name, ownership=True):
                observed_reads.append((identity, row))
            if not self._accepts_tool(name):
                continue
            if ordered is not None and identity not in ordered:
                continue
            digests = row.get("argument_digests", {})
            if not isinstance(digests, dict) or any(digests.get(key) != value for key, value in expected.items()):
                continue
            matching_execution = True
            parents = [ref for ref, parent in records.items() if self.via and _succeeded(parent, self.via)
                       and identity in parent.get("child_effect_ids", ())]
            if not self.via or parents:
                eligible.append((identity, row))
                witnesses.update((identity, *parents))
        guidance = self._evidence_guidance
        if not eligible:
            if matching_execution:
                return ToolObservation(RequirementEvaluation("tool_parent_missing",
                    "The child execution succeeded, but no successful required parent committed it. " + guidance))
            return ToolObservation(RequirementEvaluation("tool_execution_missing", guidance))
        if self.report_label and results is not None and self.browser_text is not None:
            # A report can retain several records and documents. Select its
            # own source/record/field before choosing that scope's latest read.
            eligible = _latest_report_reads(eligible, results, self.browser_text, observed=observed_reads)
            if not eligible:
                target = self.browser_text.context or self.browser_text.url
                required = ("a confirmed query result" if self.browser_text.query_result else "formal record ownership")
                return ToolObservation(RequirementEvaluation("report_record_evidence_missing",
                    f"{self.report_label}: no read establishes {required} for {target!r}. "
                    "Inspect the current observed record or its query/detail candidates. "
                    "A successful reread of an unrelated or unchanged view does not establish this field."))
            witnesses = {identity for identity, _ in eligible} | {
                ref for ref, parent in records.items() if self.via and _succeeded(parent, self.via)
                and any(identity in parent.get("child_effect_ids", ()) for identity, _ in eligible)}
        elif self.binds_browser_fields and results is not None:
            eligible = [(identity, row) for identity, row in eligible
                        if browser_document is not None
                        and (self.browser_text is None or browser_document.get("url") == self.browser_text.url)
                        and (identity not in results
                            or results[identity].audit.get("browser_document") == browser_document)]
            if not eligible:
                return ToolObservation(RequirementEvaluation("observation_selector_unavailable",
                    "No matching receipt observes the current browser page. " + guidance))
            # A later read of the same declared selector replaces the previous snapshot.
            if self._binds_latest_browser_fields:
                eligible = eligible[-1:]
            children = {identity for identity, _ in eligible}
            witnesses = children | {ref for ref, parent in records.items() if self.via and _succeeded(parent, self.via)
                                    and children.intersection(parent.get("child_effect_ids", ()))}
        outcome = (ToolObservation(RequirementEvaluation()) if self.answer_pointer is None
                   else self._observe_value(eligible, results))
        witnesses.update(prerequisite_refs)
        return replace(outcome, evaluation=replace(outcome.evaluation, evidence_refs=tuple(sorted(witnesses))))

    def _accepts_tool(self, name: str, *, ownership: bool = False) -> bool:
        if self.report_label and (ownership or not self.via):
            from codey.toolchain.constants import PAGE_DETAIL_TOOL
            from codey.toolchain.tool_spec import SOURCE_READ_TOOL_NAMES

            return name in SOURCE_READ_TOOL_NAMES | {"browser_read", PAGE_DETAIL_TOOL}
        return name == self.tool

    def _observe_value(
        self, eligible: list[tuple[str, ExecutionRecord]], results: Mapping[str, ToolResult] | None,
    ) -> ToolObservation:
        from codey.toolchain.tool_spec import canonical_tool_name

        assert self.answer_pointer is not None
        observed: list[tuple[str, JsonValue, str]] = []
        for identity, row in eligible:
            try:
                if results is None:
                    raise ValueError("full receipts unavailable")
                receipt = results[identity]
                if (not isinstance(receipt, ToolResult) or not receipt.ok
                        or not self._accepts_tool(canonical_tool_name(receipt.call.name))
                        or {key: argument_digest(value) for key, value in receipt.call.args.items()}
                        != row.get("argument_digests")):
                    raise ValueError("receipt differs from committed execution")
                if ((self._binds_browser_body or self.binds_browser_fields or self.tool == "browser_upload")
                        and receipt.call.call_id != row.get("call_id")):
                    raise ValueError("browser receipt identity differs from committed execution")
                scope = report_read_scope(receipt, self.browser_text, include_missing=True) if self.report_label and self.browser_text is not None else None
                value = ((scope.text if scope is not None else None) if self.report_label else
                         _query_body(receipt, self.browser_text) if self.browser_text is not None and self.browser_text.context else
                         _browser_body(receipt, url=self.browser_text.url if self.browser_text is not None else None)
                         if self._binds_browser_body
                         else decode_json_value(receipt.canonical["value"]))
            except (KeyError, ValueError, TypeError):
                return ToolObservation(RequirementEvaluation("observation_receipt_invalid",
                    "The required execution committed, but its full receipt is unavailable or inconsistent. "
                    "Restore the original verified receipt before proposing done; display text cannot prove the answer."
                    + (" " + self._evidence_guidance if self.binds_browser_fields else "")))
            if ((self._binds_browser_body and value is None)
                    or ((self.tool == "browser_upload" or self.answer_contains) and receipt.truncated)
                    or (self.binds_browser_fields and not self._binds_browser_body
                        and (receipt.truncated or not isinstance(value, dict)
                        or (self.tool == "browser_eval" and value.get("truncated") is not False)))):
                return ToolObservation(RequirementEvaluation("observation_selector_unavailable",
                    "The committed browser receipt or read window is incomplete. Obtain complete evidence before editing the answer. "
                    + self._evidence_guidance))
            selected: JsonValue
            try:
                if self.browser_text is not None:
                    assert isinstance(value, str)
                    if self.report_allow_missing and self.browser_text.prefix not in value:
                        observed.append((identity, None, "null"))
                        continue
                    if self.report_label and self.browser_text.prefix not in value:
                        return ToolObservation(RequirementEvaluation("report_field_not_observed",
                            f"{self.report_label}: the complete formal read of the selected record lacks this field. "
                            "Select its current observed query/detail action to investigate the missing information; "
                            "rereading the unchanged view cannot load it. If a click is already delivered, continue "
                            "its wait/read recovery without clicking again."))
                    selected = self.browser_text.select(value)
                elif self._binds_browser_body:
                    selected = value
                elif self.result_line is not None:
                    from codey.toolchain.result_schema import read_page_line

                    selected = read_page_line(value, self.result_line)
                    if selected is None:
                        continue
                else:
                    selected = json_pointer(value, self.result_pointer)
                if (self.answer_contains or self.omit_from_explanation) and not (
                        isinstance(selected, str) and selected.strip() or type(selected) in {int, float}):
                    raise ValueError("explanation containment requires nonempty text or a finite number")
                observed.append((identity, selected, _json(selected)))
            except (KeyError, ValueError, TypeError, IndexError):
                return ToolObservation(RequirementEvaluation("observation_selector_unavailable",
                    "The committed result does not contain the required result selector. " + self._evidence_guidance))
        if not observed:
            return ToolObservation(RequirementEvaluation("observation_selector_unavailable",
                "No committed page contains the required absolute line. " + self.execution_guidance))
        if any(item[2] != observed[0][2] for item in observed):
            return ToolObservation(RequirementEvaluation("conflicting_observations",
                "Committed observations disagree at the required selector. An answer edit cannot resolve this. "
                "Report the conflict; do not choose an arbitrary result or repeat execution merely to restate it."
                + (" " + self._evidence_guidance if self.binds_browser_fields else "")))
        return ToolObservation(RequirementEvaluation(), observed[0][1], tuple(item[0] for item in observed))

    @property
    def _binds_browser_body(self) -> bool:
        return self.tool == "browser_read" and self.result_pointer in {"", "/text"}

    @property
    def binds_browser_fields(self) -> bool:
        return self._binds_latest_browser_fields or (self.answer_pointer is not None and (
            (self.tool == "browser_eval" and (self.result_pointer == "/value" or self.result_pointer.startswith("/value/")))
            or (self.tool == "browser_tab_list" and self.result_pointer == "/page_url")))

    @property
    def _binds_latest_browser_fields(self) -> bool:
        return self.browser_text is not None or (self.tool == "browser_eval" and self.result_pointer.startswith("/value/"))

    @property
    def _evidence_guidance(self) -> str:
        if self.browser_text is not None:
            return (f"Read the complete current document at {self.browser_text.url} with a single value between "
                    f"{self.browser_text.prefix!r} and {self.browser_text.suffix!r}. " + self.execution_guidance)
        if self.binds_browser_fields:
            return (f"Required answer field {self.answer_pointer!r} needs current-page result selector "
                    f"{self.result_pointer!r}. " + self.execution_guidance)
        return self.execution_guidance

    def evaluate_observation(self, observation: ToolObservation, final_text: str) -> RequirementEvaluation:
        if not observation.evaluation.passed or self.answer_pointer is None:
            return observation.evaluation
        if self.report_label:
            # Research checks the Markdown and its exact passage bindings in
            # the original submission/compilation transaction.
            return observation.evaluation
        outcome = self._evaluate_answer(observation, final_text)
        return replace(outcome, evidence_refs=observation.evaluation.evidence_refs)

    def _evaluate_answer(self, observation: ToolObservation, final_text: str) -> RequirementEvaluation:
        assert self.answer_pointer is not None
        expected = observation.value
        refs = list(observation.result_refs)
        try:
            answer = decode_answer(final_text)
        except (ValueError, TypeError):
            return self._answer_failure("answer_json_invalid", expected, refs)
        try:
            actual = json_pointer(answer, self.answer_pointer)
        except (KeyError, ValueError, TypeError, IndexError):
            return self._answer_failure("answer_pointer_missing", expected, refs)
        if self.answer_contains:
            if isinstance(actual, str) and _contains_value(actual, expected):
                return RequirementEvaluation()
            result = self._answer_failure("answer_text_missing", expected, refs)
            return replace(result, feedback=result.feedback + "\n\nRejected explanation to revise:\n"
                           + (actual if isinstance(actual, str) else _value_preview(actual)))
        if _json(actual) == _json(expected):
            if self.omit_from_explanation:
                explanation = answer.get("explanation") if isinstance(answer, dict) else None
                if not isinstance(explanation, str):
                    return self._answer_failure("answer_explanation_missing", expected, refs)
                if _contains_value(explanation, expected):
                    return self._answer_failure("answer_text_forbidden", expected, refs)
            return RequirementEvaluation()
        reason = "answer_type_mismatch" if type(actual) is not type(expected) else "answer_value_mismatch"
        result = self._answer_failure(reason, expected, refs)
        return RequirementEvaluation(result.reason_code,
            result.feedback + "\nActual answer value (JSON): " + _value_preview(actual))

    def _answer_failure(self, reason: str, expected: JsonValue, refs: list[str]) -> RequirementEvaluation:
        if self.omit_from_explanation and reason in {"answer_text_forbidden", "answer_explanation_missing"}:
            return RequirementEvaluation(reason,
                "Execution and observation are already satisfied. Do not repeat tool execution. "
                "Repair only /explanation: remove the value belonging to independent field "
                f"{self.answer_pointer!r} from the prose. The application already fills that field from its receipt. "
                "Preserve supported business facts and satisfy all explanation constraints together. "
                "Submit the complete revised natural-language explanation in done.paragraphs.", tuple(refs))
        view = "output" if self._binds_browser_body else "value"
        instruction = (EXPLANATION_GUIDANCE + " Preserve the remaining analysis; include the selected text in "
                       "done.paragraphs and resubmit done. " if self.answer_contains else
                       EXPLANATION_GUIDANCE + " Preserve the remaining analysis; remove the selected value from "
                       "done.paragraphs and resubmit done. The independent field remains receipt-bound. "
                       if self.omit_from_explanation else JSON_ANSWER_GUIDANCE + " ")
        return RequirementEvaluation(reason,
            "Execution and observation are already satisfied. Do not repeat the tool execution. "
            + instruction + f" Repair only the answer at pointer {self.answer_pointer!r} "
            "using the committed selected value below. Preserve its JSON type and content, including literal quotes. "
            f'Do not copy a truncated preview; use read_tool_result with view="{view}" for omitted detail.\n'
            "Expected selected value (JSON): " + _value_preview(expected) + "\n"
            "Committed result references: " + _json(refs[:8]) + f" (total {len(refs)})")


def _contains_value(text: str, value: JsonValue) -> bool:
    numeric = type(value) in {int, float} or (isinstance(value, str) and re.fullmatch(
        r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value) is not None)
    if numeric:
        selected = value if isinstance(value, str) else _json(value)
        return re.search(_text_occurrence(selected), text) is not None
    if not isinstance(value, str) or not value.strip():
        return False
    return re.search(_text_occurrence(value), text) is not None


def _text_occurrence(selected: str) -> str:
    if re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", selected):
        return r"(?<![A-Za-z0-9_.+-])" + re.escape(selected) + r"(?![A-Za-z0-9_]|\.[0-9])"
    left = r"(?<![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", selected) else ""
    right = r"(?![A-Za-z0-9_])" if re.search(r"[A-Za-z0-9_]$", selected) else ""
    return left + re.escape(selected) + right


def omit_selected_text(text: str, selected: str) -> str:
    """Remove only a declared exact string occurrence, retaining identifier boundaries."""
    return re.sub(_text_occurrence(selected), "", text) if selected else text


def _query_body(receipt: ToolResult, selector: BrowserTextSelector) -> str | None:
    """Select only a confirmed record region covered by the formal read window."""
    from codey.research.ledger_receipts import source_observation

    region = receipt.audit.get("query_region")
    if not isinstance(region, dict) or not receipt.audit.get("query_click"):
        return None
    raw_context = region.get("context")
    context = decode_json_object(raw_context) if isinstance(raw_context, dict) else None
    text = region.get("text")
    if (context is None or context.get("label") != selector.context
            or region.get("visible") is not True or not isinstance(text, str) or not text):
        return None
    source, body, _pages = source_observation(receipt.canonical.get("research_observation"))
    if receipt.truncated or source.truncated or source.final_url != selector.url:
        return None
    passages = receipt.canonical.get("read_passages")
    if not isinstance(passages, list):
        return None
    for raw in passages:
        row = decode_json_object(raw)
        start, end = row.get("start"), row.get("end")
        if (type(start) is int and type(end) is int and 0 <= start < end <= len(body)
                and text in " ".join(body[start:end].split())):
            return text
    return None


def report_read_scope(receipt: ToolResult, selector: BrowserTextSelector, *,
                      require_complete: bool = True, include_missing: bool = False) -> SourceReadScope | None:
    """Select formal source ownership independently of an explicit after-query obligation."""
    from codey.research.ledger_receipts import source_observation
    from codey.task.web_intent import source_url_key

    try:
        source, body, _pages = source_observation(receipt.canonical.get("research_observation"))
    except ValueError:
        return None
    if (require_complete and (receipt.truncated or source.truncated)) or source_url_key(selector.url) not in {
            source_url_key(source.requested_url), source_url_key(source.final_url)}:
        return None
    scopes: list[tuple[int, int, bool]] = []
    if not selector.query_result and not receipt.audit.get("query_click"):
        if not selector.context:
            scopes.append((0, len(body), True))
        rows = receipt.audit.get("record_regions")
        matches: list[JsonObject] = [row for raw in rows if isinstance(raw, dict) for row in (decode_json_object(raw),)
                   if row.get("label") == selector.context] if isinstance(rows, list) else []
        if len(matches) == 1:
            start, end = matches[0].get("start"), matches[0].get("end")
            if type(start) is int and type(end) is int and 0 <= start < end <= len(body):
                scopes.append((start, end, matches[0].get("complete") is True))
    raw_region = receipt.audit.get("query_region")
    if isinstance(raw_region, dict) and receipt.audit.get("query_click"):
        region = decode_json_object(raw_region)
        context = region.get("context")
        target = region.get("text")
        if (region.get("visible") is True and isinstance(target, str) and target
                and (not selector.context or isinstance(context, dict) and context.get("label") == selector.context)):
            located = SourceReadScope.locate(body, target)
            if located.start is not None and located.end is not None:
                scopes.append((located.start, located.end, False))
    rows = receipt.canonical.get("read_passages")
    passages = [decode_json_object(raw) for raw in rows] if isinstance(rows, list) else []
    for start, end, complete in scopes:
        selected = body[start:end]
        if not complete and not include_missing and selector.prefix not in selected:
            continue
        covered = start
        for row in passages:
            lower, upper = row.get("start"), row.get("end")
            if type(lower) is int and type(upper) is int and lower <= covered < upper:
                covered = max(covered, upper)
        if covered >= end:
            return SourceReadScope(selected, start, end)
        if not require_complete:
            for row in passages:
                lower, upper = row.get("start"), row.get("end")
                if (type(lower) is int and type(upper) is int and max(start, lower) < min(end, upper)
                        and (not complete or selector.prefix in body[max(start, lower):min(end, upper)])):
                    return SourceReadScope(selected, start, end)
    return None


def _report_query_target(receipt: ToolResult, results: Mapping[str, ToolResult]) -> JsonObject | None:
    ref = receipt.audit.get("query_click")
    click = results.get(ref) if isinstance(ref, str) else None
    baseline = click.audit.get("query_baseline") if click is not None else None
    if not isinstance(baseline, dict) or not isinstance(target := baseline.get("target"), dict):
        return None
    return {"document": baseline.get("document"), "target": target}


def _report_field_targets(receipt: ToolResult, results: Mapping[str, ToolResult],
                          selector: BrowserTextSelector) -> list[JsonObject]:
    if (target := _report_query_target(receipt, results)) is not None:
        return [target]
    scope = report_read_scope(receipt, selector, require_complete=False)
    if scope is None or scope.start is None:
        return []
    try:
        value = selector.select(scope.text)
    except ValueError:
        return []
    start = scope.start + scope.text.index(selector.prefix) + len(selector.prefix)
    end = start + len(value)
    audit = decode_json_object(receipt.audit)
    regions = audit.get("record_regions")
    targets: list[JsonObject] = []
    for raw in regions if isinstance(regions, list) else []:
        row = decode_json_object(raw)
        if row.get("label") != selector.context:
            continue
        bindings = row.get("query_targets")
        for binding in bindings if isinstance(bindings, list) else []:
            observed = decode_json_object(binding)
            lower, upper = observed.get("start"), observed.get("end")
            if type(lower) is int and type(upper) is int and lower <= start < end <= upper:
                targets.append({"document": audit.get("browser_document"), "target": observed.get("target")})
    return targets


def _latest_report_reads(eligible: list[tuple[str, ExecutionRecord]], results: Mapping[str, ToolResult],
                         selector: BrowserTextSelector, *,
                         observed: list[tuple[str, ExecutionRecord]]) -> list[tuple[str, ExecutionRecord]]:
    """Keep regional ownership within one document of the selected source/record."""
    # A formal read can establish a new document without satisfying this field's
    # query/wait obligations. It invalidates old ownership, but cannot supply the answer.
    ownership = replace(selector, query_result=False)
    document: JsonObject | None = None
    for ref, _ in observed:
        receipt = results.get(ref)
        if receipt is None:
            continue
        current = receipt.audit.get("browser_document")
        if (same_document(current, current)
                and report_read_scope(receipt, ownership, require_complete=False, include_missing=True) is not None):
            document = decode_json_object(current)
    scoped = [(ref, row) for ref, row in eligible if ref in results
              and (document is None or same_document(results[ref].audit.get("browser_document"), document))
              and report_read_scope(results[ref], selector, require_complete=False, include_missing=True) is not None]
    latest: tuple[str, ExecutionRecord] | None = None
    for ref, row in scoped:
        receipt = results[ref]
        matches_field = report_read_scope(receipt, selector, require_complete=False) is not None
        target = _report_query_target(receipt, results)
        replaces_target = latest is not None and target is not None and target in _report_field_targets(results[latest[0]], results, selector)
        if latest is None or matches_field or replaces_target:
            latest = (ref, row)
    return [latest] if latest is not None else []


def _ordered_requirement_refs(rule: ToolSuccessRequirement, records: Mapping[str, ExecutionRecord],
                              results: Mapping[str, ToolResult], document: JsonObject | None,
                              ) -> tuple[set[str], frozenset[str]]:
    from codey.operations.browser_completion import ordered_browser_progress

    documents = [document]
    if rule.report_label and rule.browser_text is not None:
        documents = []
        for result in results.values():
            if report_read_scope(result, rule.browser_text, require_complete=False, include_missing=True) is None:
                continue
            raw = result.audit.get("browser_document")
            if isinstance(raw, dict) and (observed := decode_json_object(raw)) not in documents:
                documents.append(observed)
    eligible: set[str] = set()
    prerequisites: frozenset[str] = frozenset()
    for current_document in documents:
        progress = ordered_browser_progress(rule.tool, rule.after, records=records, results=results,
                                            document=current_document, stage=rule.stage)
        eligible.update(progress.refs)
        prerequisites |= progress.prerequisite_refs
    return eligible, prerequisites


def _browser_body(receipt: ToolResult, *, url: str | None = None) -> str | None:
    from codey.research.ledger_receipts import source_observation

    source, text, _pages = source_observation(receipt.canonical.get("research_observation"))
    passages = receipt.canonical.get("read_passages")
    if (receipt.truncated or source.truncated or (url is not None and source.final_url != url)
            or not isinstance(passages, list) or not passages):
        return None
    covered = 0
    for passage in passages:
        row = decode_json_object(passage)
        start, end = row.get("start"), row.get("end")
        if type(start) is not int or type(end) is not int or not covered <= start < end <= len(text):
            return None
        if text[covered:start].strip():
            return None
        covered = end
    return None if text[covered:].strip() else text


def _value_preview(value: JsonValue) -> str:
    encoded = _json(value)
    if len(encoded) <= 1024:
        return encoded
    return _json({"json_prefix": encoded[:1024], "truncated": True, "json_chars": len(encoded)})


def _succeeded(row: ExecutionRecord, name: str) -> bool:
    return (row.get("name") == name and row.get("ok") is True
            and row.get("execution_disposition") != "denied_before_execution")


def require_tool_success(
    tool: str, *, via: str = "", arguments: Mapping[str, object] | None = None,
    answer_pointer: str | None = None, result_pointer: str = "", result_line: int | None = None,
    answer_contains: bool = False,
    omit_from_explanation: bool = False,
    after: str = "",
    stage: int = 0,
) -> str:
    """Declare exact argument selectors, optionally bound to a successful parent."""
    return ToolSuccessRequirement(tool, via, _json(dict(arguments or {})),
                                  answer_pointer, result_pointer, result_line, answer_contains=answer_contains,
                                  omit_from_explanation=omit_from_explanation, after=after, stage=stage).declaration


def require_browser_read_field(*, url: str, prefix: str, suffix: str, answer_pointer: str,
                               answer_contains: bool = False, omit_from_explanation: bool = False) -> str:
    """Bind one literal text field from a complete current-page browser read."""
    return ToolSuccessRequirement("browser_read", "codemode", answer_pointer=answer_pointer,
        result_pointer="/text", browser_text=BrowserTextSelector(url, prefix, suffix),
        answer_contains=answer_contains, omit_from_explanation=omit_from_explanation).declaration


def require_browser_file_field(*, url: str, prefix: str, suffix: str, path: str, pointer: str, value_type: str) -> str:
    """Declare an exact current-page text copy to a typed file field."""
    return ToolSuccessRequirement("browser_read", answer_pointer=pointer, result_pointer="/text",
        browser_text=BrowserTextSelector(url, prefix, suffix), file_path=path, value_type=value_type).declaration


def require_browser_report_field(*, url: str, prefix: str, suffix: str, pointer: str, label: str,
                                 context: str = "", allow_missing: bool = False, query_result: bool = False) -> str:
    """An explicit caller requirement, not an inferred natural-language checklist."""
    return ToolSuccessRequirement("browser_read", answer_pointer=pointer, result_pointer="/text",
        browser_text=BrowserTextSelector(url, prefix, suffix, context, query_result), report_label=label,
        report_allow_missing=allow_missing).declaration


def parse_tool_success_check(check_id: str) -> ToolSuccessRequirement | None:
    if not check_id.startswith(PREFIX):
        return None
    expression = check_id.removeprefix(PREFIX)
    if _NAME.fullmatch(expression):
        return ToolSuccessRequirement(expression)
    data = parse_json_object(expression)
    if data.get("input") == "actions":
        raise ValueError("codemode.action has been removed; submit a new request using ordinary CodeMode")
    if not {"tool", "via", "args"} <= set(data) or set(data) - {"tool", "via", "args", "answer", "after", "stage"}:
        raise ValueError("invalid structured tool requirement")
    binding = data.get("answer")
    fields = {"pointer", "result_pointer", "line"}
    if "answer" in data and (not isinstance(binding, dict) or not fields <= set(binding)
                            or set(binding) - fields - {"browser_text", "contains", "omit_from_explanation", "file_path", "value_type", "report_label", "report_allow_missing"}):
        raise ValueError("invalid answer observation binding")
    browser_text = None
    if isinstance(binding, dict) and "browser_text" in binding:
        selector = decode_json_object(binding["browser_text"])
        if not {"url", "prefix", "suffix"} <= set(selector) or set(selector) - {"url", "prefix", "suffix", "context", "query_result"}:
            raise ValueError("invalid browser text selector")
        browser_text = BrowserTextSelector(require_json_string(selector["url"], "url"),
            require_json_string(selector["prefix"], "prefix"), require_json_string(selector["suffix"], "suffix"),
            require_json_string(selector.get("context", ""), "context"),
            require_json_boolean(selector.get("query_result", False), "query_result"))
    requirement = ToolSuccessRequirement(require_json_string(data["tool"], "tool"),
        require_json_string(data["via"], "via"), _json(data["args"]),
        require_json_string(binding["pointer"], "pointer") if isinstance(binding, dict) else None,
        require_json_string(binding["result_pointer"], "result_pointer") if isinstance(binding, dict) else "",
        require_json_integer(binding["line"], "line")
        if isinstance(binding, dict) and binding["line"] is not None else None, browser_text,
        require_json_boolean(binding.get("contains", False), "contains") if isinstance(binding, dict) else False,
        require_json_boolean(binding.get("omit_from_explanation", False), "omit_from_explanation")
        if isinstance(binding, dict) else False, require_json_string(data.get("after", ""), "after"),
        require_json_integer(data.get("stage", 0), "stage"),
        require_json_string(binding.get("file_path", ""), "file_path") if isinstance(binding, dict) else "",
        require_json_string(binding.get("value_type", ""), "value_type") if isinstance(binding, dict) else "",
        require_json_string(binding.get("report_label", ""), "report_label") if isinstance(binding, dict) else "",
        require_json_boolean(binding.get("report_allow_missing", False), "report_allow_missing") if isinstance(binding, dict) else False)
    if requirement.declaration != check_id:
        raise ValueError("tool requirement must use its canonical check id")
    return requirement


def _validate_pointer(pointer: str) -> None:
    if (type(pointer) is not str or len(pointer) > 512 or (pointer and not pointer.startswith("/"))
            or re.search(r"~(?![01])", pointer)):
        raise ValueError("answer selectors must be bounded JSON pointers")


def has_browser_field_bindings(checks: tuple[str, ...], *, tool: str | None = None) -> bool:
    for check in checks:
        requirement = parse_tool_success_check(check)
        if (requirement is not None and requirement.binds_browser_fields
                and (tool is None or requirement.tool == tool)):
            return True
    return False


def json_pointer(value: JsonValue, pointer: str) -> JsonValue:
    _validate_pointer(pointer)
    if not pointer:
        return value
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                raise ValueError("invalid JSON array index")
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise ValueError("JSON pointer does not select a value")
    return value


def decode_answer(text: str) -> JsonValue:
    loaded: object = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    answer = decode_json_value(loaded)
    _json(answer)
    return answer


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate answer JSON property")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")
