"""Tool-success predicates encoded in the existing immutable required checks."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import NoReturn

from codey.policies.output_requirement import EXPLANATION_GUIDANCE, JSON_ANSWER_GUIDANCE
from codey.policies.requirement import RequirementEvaluation
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

    def __post_init__(self) -> None:
        from codey.policies.network import check_fetch_url

        if not self.url or len(self.url) > 2048 or self.url != self.url.strip() or check_fetch_url(self.url, resolve=False):
            raise ValueError("browser text selectors require an eligible, explicit page URL")
        if any(not marker.strip() or len(marker) > 256 or marker.splitlines() != [marker]
               for marker in (self.prefix, self.suffix)):
            raise ValueError("browser text selectors require bounded, nonempty single-line markers")

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

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.tool) or (self.via and not _NAME.fullmatch(self.via)):
            raise ValueError("tool requirement names must be canonical tool names")
        args = parse_json_object(self.arguments_json)
        object.__setattr__(self, "arguments_json", _json(args))
        if self.answer_pointer is not None:
            _validate_pointer(self.answer_pointer)
            _validate_pointer(self.result_pointer)
        elif self.result_pointer or self.result_line is not None:
            raise ValueError("result selectors require an answer pointer")
        if type(self.answer_contains) is not bool or (self.answer_contains and self.answer_pointer != "/explanation"):
            raise ValueError("answer containment requires the /explanation string destination")
        if self.result_line is not None and (
            type(self.result_line) is not int or self.result_line < 1
            or self.tool != "read_file" or self.result_pointer != "/text"
        ):
            raise ValueError("absolute result lines require read_file /text and a positive integer")
        if self.browser_text is not None and (
            self.tool != "browser_read" or self.via != "codemode" or not self.answer_pointer
            or self.result_pointer != "/text" or self.result_line is not None
        ):
            raise ValueError("browser text selectors require browser_read via codemode and a named answer field")

    @property
    def declaration(self) -> str:
        if not self.via and self.arguments_json == "{}" and self.answer_pointer is None:
            return PREFIX + self.tool
        data: JsonObject = {"tool": self.tool, "via": self.via,
                            "args": parse_json_object(self.arguments_json)}
        if self.answer_pointer is not None:
            answer: JsonObject = {"pointer": self.answer_pointer, "result_pointer": self.result_pointer,
                                  "line": self.result_line}
            if self.browser_text is not None:
                answer["browser_text"] = {"url": self.browser_text.url, "prefix": self.browser_text.prefix,
                                          "suffix": self.browser_text.suffix}
            if self.answer_contains:
                answer["contains"] = True
            data["answer"] = answer
        return PREFIX + _json(data)

    @property
    def check_id(self) -> str:
        if not self.via and self.arguments_json == "{}" and self.answer_pointer is None:
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
        else:
            execution = (f"Required execution: call {target} and receive a successful tool result. "
                         "Displayed examples and failed calls do not satisfy this requirement.")
        return execution

    @property
    def guidance(self) -> str:
        return self.model_guidance()

    def model_guidance(self, *, explanation: bool = False) -> str:
        execution = self.execution_guidance
        if self.answer_pointer is None:
            return execution
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
        content = ("In done.summary, supply only the natural-language explanation. " if explanation else
                   "In done.summary, supply only unbound JSON fields, or {} when none are needed. ")
        evidence = ("The latest matching receipt for the current browser document must be complete."
                    if self.binds_browser_fields else "Conflicting observations cannot establish this answer requirement.")
        return (execution + " " + instruction + f" The application fills pointer {self.answer_pointer!r} "
                f"from the committed {selected}, preserving its JSON type and complete value. "
                + content + evidence)

    def evaluate(
        self, records: Mapping[str, ExecutionRecord], *, final_text: str = "", results: Mapping[str, ToolResult] | None = None,
    ) -> RequirementEvaluation:
        return self.evaluate_observation(self.observe(records, results=results), final_text)

    def observe(
        self, records: Mapping[str, ExecutionRecord], *, results: Mapping[str, ToolResult] | None = None,
        browser_document: JsonObject | None = None,
    ) -> ToolObservation:
        expected = {key: argument_digest(value)
                    for key, value in parse_json_object(self.arguments_json).items()}
        eligible: list[tuple[str, ExecutionRecord]] = []
        witnesses: set[str] = set()
        matching_execution = False
        for identity, row in records.items():
            if not _succeeded(row, self.tool):
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
        if self.binds_browser_fields and results is not None:
            eligible = [(identity, row) for identity, row in eligible
                        if browser_document is not None
                        and (self.browser_text is None or browser_document.get("url") == self.browser_text.url)
                        and (identity not in results
                            or results[identity].audit.get("browser_document") == browser_document)]
            if not eligible:
                return ToolObservation(RequirementEvaluation("observation_selector_unavailable",
                    "No matching receipt observes the current browser page. " + guidance))
            # A later read of the same declared selector replaces the previous snapshot.
            eligible = eligible[-1:]
            children = {identity for identity, _ in eligible}
            witnesses = children | {ref for ref, parent in records.items() if self.via and _succeeded(parent, self.via)
                                    and children.intersection(parent.get("child_effect_ids", ()))}
        outcome = (ToolObservation(RequirementEvaluation()) if self.answer_pointer is None
                   else self._observe_value(eligible, results))
        return replace(outcome, evaluation=replace(outcome.evaluation, evidence_refs=tuple(sorted(witnesses))))

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
                        or canonical_tool_name(receipt.call.name) != self.tool
                        or {key: argument_digest(value) for key, value in receipt.call.args.items()}
                        != row.get("argument_digests")):
                    raise ValueError("receipt differs from committed execution")
                if ((self._binds_browser_body or self.binds_browser_fields or self.tool == "browser_upload")
                        and receipt.call.call_id != row.get("call_id")):
                    raise ValueError("browser receipt identity differs from committed execution")
                value = (_browser_body(receipt, url=self.browser_text.url if self.browser_text is not None else None)
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
                        or value.get("truncated") is not False))):
                return ToolObservation(RequirementEvaluation("observation_selector_unavailable",
                    "The committed browser receipt or read window is incomplete. Obtain complete evidence before editing the answer. "
                    + self._evidence_guidance))
            selected: JsonValue
            try:
                if self.browser_text is not None:
                    assert isinstance(value, str)
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
                if self.answer_contains and not (
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
            return RequirementEvaluation()
        reason = "answer_type_mismatch" if type(actual) is not type(expected) else "answer_value_mismatch"
        result = self._answer_failure(reason, expected, refs)
        return RequirementEvaluation(result.reason_code,
            result.feedback + "\nActual answer value (JSON): " + _value_preview(actual))

    def _answer_failure(self, reason: str, expected: JsonValue, refs: list[str]) -> RequirementEvaluation:
        view = "output" if self._binds_browser_body else "value"
        instruction = (EXPLANATION_GUIDANCE + " Preserve the remaining analysis; include the selected text in "
                       "done.summary and resubmit done. " if self.answer_contains else JSON_ANSWER_GUIDANCE + " ")
        return RequirementEvaluation(reason,
            "Execution and observation are already satisfied. Do not repeat the tool execution. "
            + instruction + f" Repair only the answer at pointer {self.answer_pointer!r} "
            "using the committed selected value below. Preserve its JSON type and content, including literal quotes. "
            f'Do not copy a truncated preview; use read_tool_result with view="{view}" for omitted detail.\n'
            "Expected selected value (JSON): " + _value_preview(expected) + "\n"
            "Committed result references: " + _json(refs[:8]) + f" (total {len(refs)})")


def _contains_value(text: str, value: JsonValue) -> bool:
    if type(value) in {int, float}:
        return re.search(r"(?<![A-Za-z0-9_.+-])" + re.escape(_json(value))
                         + r"(?![A-Za-z0-9_]|\.[0-9])", text) is not None
    if not isinstance(value, str) or not value.strip():
        return False
    selected = value
    # Keep ASCII identifiers whole, while allowing surrounding Chinese prose.
    left = r"(?<![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", selected) else ""
    right = r"(?![A-Za-z0-9_])" if re.search(r"[A-Za-z0-9_]$", selected) else ""
    return re.search(left + re.escape(selected) + right, text) is not None


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
) -> str:
    """Declare exact argument selectors, optionally bound to a successful parent."""
    return ToolSuccessRequirement(tool, via, _json(dict(arguments or {})),
                                  answer_pointer, result_pointer, result_line, answer_contains=answer_contains).declaration


def require_browser_read_field(*, url: str, prefix: str, suffix: str, answer_pointer: str,
                               answer_contains: bool = False) -> str:
    """Bind one literal text field from a complete current-page browser read."""
    return ToolSuccessRequirement("browser_read", "codemode", answer_pointer=answer_pointer,
        result_pointer="/text", browser_text=BrowserTextSelector(url, prefix, suffix),
        answer_contains=answer_contains).declaration


def parse_tool_success_check(check_id: str) -> ToolSuccessRequirement | None:
    if not check_id.startswith(PREFIX):
        return None
    expression = check_id.removeprefix(PREFIX)
    if _NAME.fullmatch(expression):
        return ToolSuccessRequirement(expression)
    data = parse_json_object(expression)
    if set(data) not in ({"tool", "via", "args"}, {"tool", "via", "args", "answer"}):
        raise ValueError("invalid structured tool requirement")
    binding = data.get("answer")
    fields = {"pointer", "result_pointer", "line"}
    if "answer" in data and (not isinstance(binding, dict) or set(binding) not in (
            fields, fields | {"browser_text"}, fields | {"contains"}, fields | {"browser_text", "contains"})):
        raise ValueError("invalid answer observation binding")
    browser_text = None
    if isinstance(binding, dict) and "browser_text" in binding:
        selector = decode_json_object(binding["browser_text"])
        if set(selector) != {"url", "prefix", "suffix"}:
            raise ValueError("invalid browser text selector")
        browser_text = BrowserTextSelector(*(require_json_string(selector[key], key) for key in ("url", "prefix", "suffix")))
    requirement = ToolSuccessRequirement(require_json_string(data["tool"], "tool"),
        require_json_string(data["via"], "via"), _json(data["args"]),
        require_json_string(binding["pointer"], "pointer") if isinstance(binding, dict) else None,
        require_json_string(binding["result_pointer"], "result_pointer") if isinstance(binding, dict) else "",
        require_json_integer(binding["line"], "line")
        if isinstance(binding, dict) and binding["line"] is not None else None, browser_text,
        require_json_boolean(binding.get("contains", False), "contains") if isinstance(binding, dict) else False)
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
