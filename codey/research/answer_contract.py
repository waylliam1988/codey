"""Whole-task research submissions, derived from original settled read receipts.

These values contain no scheduler, mutable alias registry, or knowledge writes.
The ordinary kernel freezes them in its existing provider request and effect.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from codey.knowledge.model import canonical_json
from codey.research.questions import INVESTIGATION_TOOLS

ANSWER_SCOPE_VERSION = 4
ANSWER_CONTRACT_VERSION = 1
READ_BUDGET = 18000
MAX_ANSWER_CHARACTERS = 64000
ANSWER_STATES = ("answered", "partial", "unresolved")
ANSWER_TOOLS = ("web_search", "open_url", "open_result", "reopen_source", "open_hit", "source_search",
                "read_tool_result", "submit_research_answer", "ask_user")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class AnswerScope:
    task_digest: str
    aliases: tuple[tuple[str, str], ...]
    attempt_refs: tuple[str, ...]
    schema_version: int = ANSWER_SCOPE_VERSION

    @property
    def passage_refs(self) -> tuple[str, ...]:
        return tuple(ref for _, ref in self.aliases)

    @property
    def replaces(self) -> str:
        return ""

    @property
    def submission_mode(self) -> str:
        return "document"

    def to_payload(self) -> dict[str, Any]:
        return json.loads(canonical_json(asdict(self)))

    @classmethod
    def from_payload(cls, raw: Any) -> AnswerScope:
        if (not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__)
                or raw.get("schema_version") != ANSWER_SCOPE_VERSION
                or not isinstance(raw.get("task_digest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", raw["task_digest"]) is None
                or not isinstance(raw.get("aliases"), list) or not isinstance(raw.get("attempt_refs"), list)):
            raise ValueError("invalid whole-task research scope")
        for row in raw["aliases"]:
            if (not isinstance(row, list) or len(row) != 2 or not all(isinstance(v, str) for v in row)
                    or re.fullmatch(r"e[1-9][0-9]*", row[0]) is None
                    or re.fullmatch(r"p[0-9a-f]{12}", row[1]) is None):
                raise ValueError("invalid frozen evidence alias")
        if (len({row[0] for row in raw["aliases"]}) != len(raw["aliases"])
                or len({row[1] for row in raw["aliases"]}) != len(raw["aliases"])
                or any(not isinstance(ref, str) or not ref for ref in raw["attempt_refs"])
                or len(set(raw["attempt_refs"])) != len(raw["attempt_refs"])):
            raise ValueError("duplicate or invalid frozen research identity")
        return cls(raw["task_digest"], tuple(tuple(row) for row in raw["aliases"]), tuple(raw["attempt_refs"]))


def answer_scope(session: Any, *, budget: int = READ_BUDGET) -> AnswerScope:
    """Append-only labels from receipt order; only complete visible windows fit."""
    labels: dict[str, str] = {}
    lengths: dict[str, int] = {}
    windows = []
    attempts = []
    results = getattr(session, "_memory_results", {})
    for identity, result in results.items():
        if result.call.name in INVESTIGATION_TOOLS:
            attempts.append(identity)
        if not result.ok:
            continue
        original = results.get(result.call.args.get("result_ref")) if result.call.name == "read_tool_result" else result
        rows = original.canonical.get("read_passages", []) if original is not None and original.ok else []
        window = []
        for row in rows:
            ref = row["ref"]
            labels.setdefault(ref, f"e{len(labels) + 1}")
            lengths[ref] = row["end"] - row["start"]
            window.append(ref)
        if window:
            windows.append(window)
    selected: set[str] = set()
    size = 0
    for window in reversed(windows):
        extra = set(window) - selected
        cost = sum(lengths[ref] for ref in extra)
        if size + cost <= budget:
            selected.update(extra)
            size += cost
    return AnswerScope(digest(session.task_text), tuple((label, ref) for ref, label in labels.items() if ref in selected),
                       tuple(attempts))


def citation_occurrences(markdown: str) -> list[dict[str, Any]]:
    """Record explicit prose citations; code examples do not select evidence."""
    masked = re.sub(r"(?ms)^([ \t]*)(`{3,}|~{3,})[^\n]*\n.*?^\1\2[^\n]*(?:\n|$)",
                    lambda match: " " * len(match[0]), markdown)
    masked = re.sub(r"(`+)([^`\n]*?)\1", lambda match: " " * len(match[0]), masked)
    rows = []
    for match in re.finditer(r"(?<!\\)\[(e[0-9][^\]\n]*)\](?!\()", masked):
        label = match[1]
        if re.fullmatch(r"e[1-9][0-9]*", label) is None:
            raise ValueError("Use individual evidence citations such as [e1][e2], exactly as displayed.")
        rows.append({"label": label, "start": match.start(), "end": match.end()})
    return rows


def submit_answer(scope: AnswerScope, args: dict[str, Any]) -> dict[str, Any]:
    if (set(args) != {"markdown", "state"} or args.get("state") not in ANSWER_STATES
            or not isinstance(args.get("markdown"), str)
            or not 12 <= len(args["markdown"].strip()) <= MAX_ANSWER_CHARACTERS):
        raise ValueError("Submit markdown (12..64000 characters) and state=answered/partial/unresolved.")
    markdown = args["markdown"].strip()
    citations = citation_occurrences(markdown)
    aliases = dict(scope.aliases)
    unknown = {row["label"] for row in citations} - aliases.keys()
    if unknown:
        raise ValueError("Unknown or unshown evidence: " + ", ".join(sorted(unknown))
                         + ". Use only the displayed [eN] labels, or read the original material first.")
    if not scope.attempt_refs:
        raise ValueError("Investigate the task before submitting; failed acquisition attempts may support a gap report.")
    if not citations and args["state"] != "unresolved":
        raise ValueError("An answered or partial report needs explicit [eN] evidence citations. Describe an unresolved gap otherwise.")
    body = {"schema_version": ANSWER_CONTRACT_VERSION, "task_digest": scope.task_digest,
            "markdown": markdown, "state": args["state"], "scope": scope.to_payload(),
            "citations": [{**row, "passage_ref": aliases[row["label"]]} for row in citations]}
    return {**body, "submission_digest": digest(body)}


def validate_submission(raw: Any, task: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("task_digest") != digest(task):
        raise ValueError("research submission belongs to another task")
    scope = AnswerScope.from_payload(raw.get("scope"))
    expected = submit_answer(scope, {"markdown": raw.get("markdown"), "state": raw.get("state")})
    if raw != expected:
        raise ValueError("research submission differs from its frozen input")
    return expected


def submitted_answer(session: Any) -> dict[str, Any] | None:
    selected = None
    for result in getattr(session, "_memory_results", {}).values():
        if result.ok and result.call.name == "submit_research_answer" and "research_submission" in result.canonical:
            selected = validate_submission(result.canonical["research_submission"], session.task_text)
    return selected


def answer_spec(spec: Any, scope: AnswerScope) -> Any:
    if spec.name != "submit_research_answer":
        parameters = {key: value for key, value in spec.parameters if key != "question_ref"}
        examples = []
        for raw in spec.json_examples:
            example = json.loads(raw)
            example.get("args", {}).pop("question_ref", None)
            examples.append(canonical_json(example))
        description = spec.description.split(" Select question_ref=")[0]
        return replace(spec, parameters=tuple(parameters.items()), required=tuple(k for k in spec.required if k != "question_ref"),
                       json_examples=tuple(examples), description=description)
    parameters = {"markdown": {"type": "string", "minLength": 12, "maxLength": MAX_ANSWER_CHARACTERS},
                  "state": {"type": "string", "enum": list(ANSWER_STATES)}}
    return replace(spec, parameters=tuple(parameters.items()), required=("markdown", "state"),
        description="Submit the whole research answer as Markdown, with explicit [eN] citations beside supported statements "
                    "and clear remaining gaps. state is your declaration, not semantic certification. "
                    "The application validates and publishes this answer; no separate plan, finding, adoption or done is needed.",
        json_examples=(canonical_json({"tool": spec.name, "args": {"state": "partial" if scope.aliases else "unresolved",
            "markdown": ("Supported answer [" + scope.aliases[0][0] + "]. Remaining uncertainty: ...") if scope.aliases else
                        "The attempted investigation could not establish the answer. Remaining gap: ..."}}),))
