"""Tool-success predicates encoded in the existing immutable required checks."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codey.policies.requirement import RequirementEvaluation
from codey.runtime.core.models import ToolResult

PREFIX = "tool_succeeded:"
_NAME = re.compile(r"[a-z][a-z0-9_.]{0,63}\Z")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def argument_digest(value: object) -> str:
    """Type-preserving selectors; do not retain file bodies in the projection."""
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolSuccessRequirement:
    tool: str
    via: str = ""
    arguments_json: str = "{}"
    answer_pointer: str | None = None
    result_pointer: str = ""
    result_line: int | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.tool) or (self.via and not _NAME.fullmatch(self.via)):
            raise ValueError("tool requirement names must be canonical tool names")
        args = json.loads(self.arguments_json)
        if not isinstance(args, dict):
            raise ValueError("tool requirement arguments must be an object")
        object.__setattr__(self, "arguments_json", _json(args))
        if self.answer_pointer is not None:
            _validate_pointer(self.answer_pointer)
            _validate_pointer(self.result_pointer)
        elif self.result_pointer or self.result_line is not None:
            raise ValueError("result selectors require an answer pointer")
        if self.result_line is not None and (
            type(self.result_line) is not int or self.result_line < 1
            or self.tool != "read_file" or self.result_pointer != "/text"
        ):
            raise ValueError("absolute result lines require read_file /text and a positive integer")

    @property
    def declaration(self) -> str:
        if not self.via and self.arguments_json == "{}" and self.answer_pointer is None:
            return PREFIX + self.tool
        data = {"tool": self.tool, "via": self.via, "args": json.loads(self.arguments_json)}
        if self.answer_pointer is not None:
            data["answer"] = {"pointer": self.answer_pointer, "result_pointer": self.result_pointer,
                              "line": self.result_line}
        return PREFIX + _json(data)

    @property
    def check_id(self) -> str:
        if not self.via and self.arguments_json == "{}" and self.answer_pointer is None:
            return self.declaration
        # Completion proofs contain bounded identifiers, never raw selectors.
        return PREFIX + hashlib.sha256(self.declaration.encode("utf-8")).hexdigest()[:32]

    @property
    def guidance(self) -> str:
        target = f"{self.tool} with arguments matching {self.arguments_json}"
        if self.via:
            execution = (f"Required execution: inside {self.via}, call tools.{target}. "
                    "The child call and its parent must both commit successfully. "
                    f"A separate top-level {self.tool} call does not satisfy this requirement.")
        else:
            execution = f"Required execution: {target} must commit successfully."
        if self.answer_pointer is None:
            return execution
        selected = f"value{self.result_pointer}"
        if self.result_line is not None:
            selected += f", absolute file line {self.result_line}"
        return (execution + f" Final answer must be JSON: at pointer {self.answer_pointer!r}, return exactly "
                f"the observed {selected} from this committed result. Do not shorten or invent it. "
                "Use the stored result rather than repeating execution to restate it. "
                "Conflicting observations cannot establish this answer requirement.")

    def evaluate(
        self, records: Mapping[str, Any], *, final_text: str = "", results: Mapping[str, ToolResult] | None = None,
    ) -> RequirementEvaluation:
        expected = {key: argument_digest(value) for key, value in json.loads(self.arguments_json).items()}
        eligible = []
        matching_execution = False
        for identity, row in records.items():
            if not _succeeded(row, self.tool):
                continue
            digests = row.get("argument_digests", {})
            if not isinstance(digests, dict) or any(digests.get(key) != value for key, value in expected.items()):
                continue
            matching_execution = True
            if not self.via or any(
                _succeeded(parent, self.via) and identity in parent.get("child_effect_ids", ())
                for parent in records.values()
            ):
                eligible.append((identity, row))
        if not eligible:
            if matching_execution:
                return RequirementEvaluation("tool_parent_missing",
                    "The child execution succeeded, but no successful required parent committed it. " + self.guidance)
            return RequirementEvaluation("tool_execution_missing", self.guidance)
        if self.answer_pointer is None:
            return RequirementEvaluation()
        return self._evaluate_answer(eligible, final_text, results)

    def _evaluate_answer(
        self, eligible: list[tuple[str, Any]], final_text: str, results: Mapping[str, ToolResult] | None,
    ) -> RequirementEvaluation:
        from codey.toolchain.tool_spec import canonical_tool_name

        assert self.answer_pointer is not None
        observed: list[tuple[str, Any, str]] = []
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
                value = receipt.canonical["value"]
            except (KeyError, ValueError, TypeError):
                return RequirementEvaluation("observation_receipt_invalid",
                    "The required execution committed, but its full receipt is unavailable or inconsistent. "
                    "Restore the original verified receipt before proposing done; display text cannot prove the answer.")
            try:
                if self.result_line is not None:
                    from codey.toolchain.result_schema import read_page_line

                    selected: object = read_page_line(value, self.result_line)
                    if selected is None:
                        continue
                else:
                    selected = _pointer(value, self.result_pointer)
                observed.append((identity, selected, _json(selected)))
            except (KeyError, ValueError, TypeError, IndexError):
                return RequirementEvaluation("observation_selector_unavailable",
                    "The committed result does not contain the required result selector. " + self.guidance)
        if not observed:
            return RequirementEvaluation("observation_selector_unavailable",
                "No committed page contains the required absolute line. " + self.guidance)
        if any(item[2] != observed[0][2] for item in observed):
            return RequirementEvaluation("conflicting_observations",
                "Committed observations disagree at the required selector. An answer edit cannot resolve this. "
                "Report the conflict; do not choose an arbitrary result or repeat execution merely to restate it.")
        expected = observed[0][1]
        refs = [item[0] for item in observed]
        try:
            answer = json.loads(final_text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            _json(answer)  # Reject non-finite numbers produced by exponent overflow too.
        except (ValueError, TypeError):
            return self._answer_failure("answer_json_invalid", expected, refs)
        try:
            actual = _pointer(answer, self.answer_pointer)
        except (KeyError, ValueError, TypeError, IndexError):
            return self._answer_failure("answer_pointer_missing", expected, refs)
        if _json(actual) == observed[0][2]:
            return RequirementEvaluation()
        reason = "answer_type_mismatch" if type(actual) is not type(expected) else "answer_value_mismatch"
        result = self._answer_failure(reason, expected, refs)
        return RequirementEvaluation(result.reason_code,
            result.feedback + "\nActual answer value (JSON): " + _value_preview(actual))

    def _answer_failure(self, reason: str, expected: Any, refs: list[str]) -> RequirementEvaluation:
        return RequirementEvaluation(reason,
            "Execution and observation are already satisfied. Do not repeat the tool execution. "
            "Final answer must be JSON, as one bare document without Markdown fences or surrounding prose: "
            f"repair only the answer at pointer {self.answer_pointer!r} "
            "using the committed selected value below. Preserve its JSON type and content, including literal quotes. "
            'Do not copy a truncated preview; use read_tool_result with view="value" for omitted detail.\n'
            "Expected selected value (JSON): " + _value_preview(expected) + "\n"
            "Committed result references: " + _json(refs[:8]) + f" (total {len(refs)})")


def _value_preview(value: Any) -> str:
    encoded = _json(value)
    if len(encoded) <= 1024:
        return _json({"value": value})
    return _json({"json_prefix": encoded[:1024], "truncated": True, "json_chars": len(encoded)})


def _succeeded(row: Any, name: str) -> bool:
    return (isinstance(row, dict) and row.get("name") == name and row.get("ok") is True
            and row.get("execution_disposition") != "denied_before_execution")


def require_tool_success(
    tool: str, *, via: str = "", arguments: Mapping[str, object] | None = None,
    answer_pointer: str | None = None, result_pointer: str = "", result_line: int | None = None,
) -> str:
    """Declare exact argument selectors, optionally bound to a successful parent."""
    return ToolSuccessRequirement(tool, via, _json(dict(arguments or {})),
                                  answer_pointer, result_pointer, result_line).declaration


def parse_tool_success_check(check_id: str) -> ToolSuccessRequirement | None:
    if not check_id.startswith(PREFIX):
        return None
    expression = check_id.removeprefix(PREFIX)
    if _NAME.fullmatch(expression):
        return ToolSuccessRequirement(expression)
    data = json.loads(expression)
    if (not isinstance(data, dict) or set(data) not in ({"tool", "via", "args"}, {"tool", "via", "args", "answer"})
            or not isinstance(data["tool"], str) or not isinstance(data["via"], str)):
        raise ValueError("invalid structured tool requirement")
    binding = data.get("answer")
    if "answer" in data and (not isinstance(binding, dict) or set(binding) != {"pointer", "result_pointer", "line"}):
        raise ValueError("invalid answer observation binding")
    requirement = ToolSuccessRequirement(data["tool"], data["via"], _json(data["args"]),
        binding["pointer"] if binding is not None else None,
        binding["result_pointer"] if binding is not None else "", binding["line"] if binding is not None else None)
    if requirement.declaration != check_id:
        raise ValueError("tool requirement must use its canonical check id")
    return requirement


def _validate_pointer(pointer: str) -> None:
    if (type(pointer) is not str or len(pointer) > 512 or (pointer and not pointer.startswith("/"))
            or re.search(r"~(?![01])", pointer)):
        raise ValueError("answer selectors must be bounded JSON pointers")


def _pointer(value: Any, pointer: str) -> Any:
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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate answer JSON property")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")
