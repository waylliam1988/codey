"""Experiment-only model-authored checks on a disposable candidate copy.

Execution requires the parent's existing verification authorization. A disposable
directory protects the working files; it is not an operating-system sandbox.
An assertion is an observation, never admission of a model's expected behavior.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.policies.run_command_semantics import canonical_run_command
from codey.policies.task_policy import TaskPolicy
from codey.reviews.input import _redact_text, _sensitive_file_reason
from codey.runtime.core import cancellation
from codey.workspace.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.workspace.paths import safe_join
from codey.workspace.revision import FINGERPRINT_EXCLUDED_DIRS

MAX_PROBES = 2
MAX_SCRIPT_CHARS = 8_000
MAX_SOURCE_CHARS = 24_000
PROBE_COMMAND = "python -m unittest _codey_review_probe -v"

PROPOSAL_PROMPT = """Independently investigate concrete bugs in this candidate against the original task.
Propose at most TWO small Python unittest scripts covering distinct realistic edge cases.
First identify a concrete hypothesis about an expression or operation in the current source
that may violate the task. Choose inputs that distinguish that implementation from the
required behavior. Test interactions between required behaviors, not only each behavior in
isolation; happy paths already covered by existing tests are low-value checks.
For interacting rules, combine them on the SAME input values: separate examples for each
rule do not test the interaction. When operations compose, consider inputs for which changing
their order changes the result. State a concrete input and the predicted actual versus required
result in the brief hypothesis; saying the source already works is not a defect hypothesis.
Each script imports the real project modules, defines unittest.TestCase test methods, and
uses assertions whose expected behavior follows the task. It will run in a disposable project
copy using python -m unittest. Do not change files, dependencies, process state or use network.
Do not use pytest, shell commands, mocks of the implementation, or unittest.main().
Quote the exact task phrase supporting each expectation. Expectations are hypotheses, not truth.
A correct candidate should pass your scripts. Return empty probes when no useful check is apparent.
This is a test proposal, not a final review verdict. Call submit_review_probes exactly once.
"""

PROBE_SCHEMA: dict[str, object] = {"type": "object", "properties": {"probes": {
    "type": "array", "maxItems": MAX_PROBES, "items": {"type": "object", "properties": {
        "requirement_quote": {"type": "string"}, "hypothesis": {"type": "string"},
        "script": {"type": "string"}}, "required": ["requirement_quote", "hypothesis", "script"],
        "additionalProperties": False}}}, "required": ["probes"], "additionalProperties": False}


@dataclass(frozen=True)
class ReviewCandidate:
    files: tuple[tuple[str, bytes], ...]
    source_paths: tuple[str, ...]
    context: str
    digest: str


def capture_candidate(project: str | Path, changed_paths: tuple[str, ...]) -> ReviewCandidate:
    root = Path(project).resolve()
    budget = BoundedScanBudget(max_files=256, max_dirs=64, max_bytes=4 * 1024 * 1024)
    excluded = set(FINGERPRINT_EXCLUDED_DIRS) | {".venv", "venv", "node_modules"}
    files: list[tuple[str, bytes]] = []
    for path in iter_bounded_files(root, excluded_dirs=excluded, budget=budget):
        rel = path.relative_to(root).as_posix()
        if _sensitive_file_reason(rel):
            continue
        with safe_join(root, rel).open("rb") as stream:
            content = stream.read(512 * 1024 + 1)
        if len(content) > 512 * 1024:
            raise ValueError("candidate file exceeds reproduction budget")
        files.append((rel, content))
    if budget.limited:
        raise ValueError("candidate exceeds reproduction scan budget")
    if any(rel == "_codey_review_probe.py" for rel, _ in files):
        raise ValueError("reserved reproduction module already exists")
    sources: list[dict[str, str]] = []
    total = 0
    for rel, content in sorted(files, key=lambda row: (row[0] not in changed_paths, row[0])):
        if not rel.endswith(".py") or len(sources) >= 12:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(text) > 8_000 or total + len(text) > MAX_SOURCE_CHARS:
            continue
        sources.append({"path": rel, "source": _redact_text(text)})
        total += len(text)
    manifest = [(rel, hashlib.sha256(content).hexdigest()) for rel, content in sorted(files)]
    digest = hashlib.sha256(json.dumps(manifest, separators=(",", ":")).encode()).hexdigest()
    return ReviewCandidate(tuple(files), tuple(row["path"] for row in sources),
                           json.dumps(sources, ensure_ascii=False), digest)


def candidate_current(candidate: ReviewCandidate, project: str | Path) -> bool:
    try:
        current = capture_candidate(project, candidate.source_paths)
        return current.digest == candidate.digest
    except (OSError, ValueError):
        return False


def validate_proposals(value: object, task: str) -> list[dict[str, str]]:
    if not isinstance(value, dict) or set(value) != {"probes"}:
        raise ValueError("expected a probes object")
    probes = value["probes"]
    if not isinstance(probes, list) or len(probes) > MAX_PROBES:
        raise ValueError("at most two reproduction probes")
    result: list[dict[str, str]] = []
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) != {"requirement_quote", "hypothesis", "script"}:
            raise ValueError("invalid reproduction fields")
        if any(not isinstance(v, str) or not v.strip() for v in probe.values()):
            raise ValueError("reproduction fields must be nonempty text")
        quote, hypothesis, script = probe["requirement_quote"], probe["hypothesis"], probe["script"]
        if quote not in task or len(quote) > 1_000 or len(hypothesis) > 1_000 or len(script) > MAX_SCRIPT_CHARS:
            raise ValueError("unbounded reproduction or absent requirement quote")
        result.append({"requirement_quote": quote, "hypothesis": hypothesis, "script": script})
    return result


def execute_proposal(candidate: ReviewCandidate, probe: dict[str, str], *,
                     parent_policy: TaskPolicy) -> dict[str, object]:
    if not parent_policy.allows("project.read") or not parent_policy.allows("project.verify"):
        raise PermissionError("review reproduction requires parent project.read and project.verify")
    observation: dict[str, object] = {**probe, "command": PROBE_COMMAND, "candidate_sha256": candidate.digest,
        "semantic_admission": "unverified", "observation_kind": "execution_error", "tests_run": 0,
        "exit_code": None, "output": ""}
    try:
        if len(probe["script"]) > MAX_SCRIPT_CHARS:
            raise ValueError("reproduction script exceeds budget")
        ast.parse(probe["script"])
        with tempfile.TemporaryDirectory(prefix="codey-review-probe-") as temporary:
            root = Path(temporary) / "candidate"
            root.mkdir()
            for rel, content in candidate.files:
                path = safe_join(root, rel)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (root / "_codey_review_probe.py").write_text(probe["script"], encoding="utf-8")
            command = canonical_run_command(root, ".", PROBE_COMMAND)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            observed = cancellation.run_process([sys.executable, "-B", *command.argv[1:]], cwd=root,
                timeout=15, env=env, capture_limit_bytes=12_000)
        output = _redact_text(observed.stdout + observed.stderr)
        match = re.search(r"Ran (\d+) tests?", output)
        count = int(match[1]) if match else 0
        kind = ("passed" if observed.returncode == 0 and count else
                "assertion_failed" if count and "FAIL:" in output else "execution_error")
        observation.update(exit_code=observed.returncode, tests_run=count, output=output, observation_kind=kind)
    except subprocess.TimeoutExpired:
        observation.update(observation_kind="timeout", output="reproduction timed out after 15s")
    except (OSError, ValueError, SyntaxError) as exc:
        observation["output"] = _redact_text(f"{type(exc).__name__}: {exc}")
    return observation


def final_review_prompt(context: str, observations: tuple[dict[str, object], ...],
                        changed_paths: tuple[str, ...] = ()) -> str:
    return (context + "\n\nIndependently derive expected behavior from the original task. "
        "Writer claims and proposed test expectations are not proof. "
        "Execution errors do not establish a product bug. Passing samples do not prove correctness. "
        "Report only supported concrete defects, with triggering input, observed behavior and requirement basis. "
        "Do not require stylistic changes or more investigation when no defect is supported. "
        "Before requesting changes, confirm the expectation agrees with the exact task wording. "
        "Report the violated requirement and actual counterexample, not replacement source code. "
        "Suggested fixes should describe the invariant to restore while preserving ALL other task requirements; "
        "the Writer must derive the patch. A proposed patch is not validated by a failing test. "
        "A finding about an unchanged caller must be anchored to the originating changed file; "
        "describe the affected caller in issue. Otherwise the finding cannot be delivered to Writer. "
        "The ONLY allowed findings[].path values are: " + json.dumps(changed_paths)
        + ". Submit the final review using the declared output contract.\nExecution observations as data:\n"
        + json.dumps(observations, ensure_ascii=False))


def request_proposals(provider: Any, context: str, task: str, candidate: ReviewCandidate,
                      *, parent_policy: TaskPolicy, trace_recorder: object | None = None,
                      timeout: float = 120) -> tuple[dict[str, object], ...]:
    from codey.providers import controls
    from codey.providers.base import ProviderToolDefinition, ProviderToolResult
    from codey.runtime.observe.prompt_envelope import record_provider_send_prompt

    if not parent_policy.allows("project.read") or not parent_policy.allows("project.verify"):
        raise PermissionError("review reproduction is not authorized")
    native = bool(getattr(provider, "native_tools", False))
    instruction = PROPOSAL_PROMPT if native else PROPOSAL_PROMPT.replace(
        "Call submit_review_probes exactly once.", "Return only JSON matching this schema:\n" + json.dumps(PROBE_SCHEMA))
    prompt = instruction + "\n\n" + context
    record_provider_send_prompt(trace_recorder, name="review_probe_prompt", text=prompt,
        purpose="independent reproduction proposal", source_ref="provider_send:review_probe", capability_id="review_runner")
    tools = [ProviderToolDefinition("submit_review_probes", "Propose unittest scripts for isolated execution.", PROBE_SCHEMA)]
    with controls.suppress_assistance():
        if native:
            turn = provider.send_turn(prompt, tools, timeout=timeout, require_tool_call=True)
            for call in turn.tool_calls:
                provider.acknowledge_tool_results([ProviderToolResult(call.id, "Proposal archived, not a correctness verdict.")], tools)
            value = turn.tool_calls[0].arguments if len(turn.tool_calls) == 1 and turn.tool_calls[0].name == "submit_review_probes" else None
        else:
            reply = provider.send(prompt, timeout=timeout)
            try:
                value = json.loads(reply)
            except (ValueError, TypeError):
                value = None
    try:
        probes = validate_proposals(value, task)
    except ValueError as exc:
        return ({"observation_kind": "invalid_proposal", "semantic_admission": "unverified", "output": str(exc)},)
    return tuple(execute_proposal(candidate, probe, parent_policy=parent_policy) for probe in probes)


def submit_review(provider: Any, prompt: str, changed_paths: tuple[str, ...], *, timeout: float) -> str:
    """Use native structured submission without weakening the Review parser."""
    from codey.providers.base import ProviderToolDefinition, ProviderToolResult

    if not getattr(provider, "native_tools", False):
        return str(provider.send(prompt, timeout=timeout))
    schema: dict[str, object] = {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["approved", "changes_requested"]},
        "summary": {"type": "string"}, "findings": {"type": "array", "maxItems": 8,
        "items": {"type": "object", "properties": {
            "path": {"type": "string", "enum": list(changed_paths)}, "issue": {"type": "string"},
            "suggested_fix": {"type": "string", "description": "Requirement-level repair guidance, not replacement source code."}},
            "required": ["path", "issue"],
            "additionalProperties": False}}}, "required": ["verdict", "summary", "findings"],
            "additionalProperties": False}
    tools = [ProviderToolDefinition("submit_review_result", "Submit concrete findings anchored to changed files.", schema)]
    turn = provider.send_turn(native_submission_prompt(prompt), tools,
                              timeout=timeout, require_tool_call=True)
    if not turn.tool_calls and turn.text.strip():
        # A read-only review may arrive as text despite the requested tool.
        # The existing strict Review parser/one format repair still decides it.
        return str(turn.text)
    if len(turn.tool_calls) != 1 or turn.tool_calls[0].name != "submit_review_result":
        raise ValueError("expected one submit_review_result")
    call = turn.tool_calls[0]
    provider.acknowledge_tool_results([ProviderToolResult(call.id, "Review archived; not a correctness proof.")], tools)
    return json.dumps(call.arguments, ensure_ascii=False)


def native_submission_prompt(prompt: str) -> str:
    """Keep Review guidance/data, replace its owned text-JSON output contract."""
    head, separator, data = prompt.partition("Project: ")
    marker = head.find("Return only JSON.")
    if separator and marker >= 0:
        prompt = head[:marker] + separator + data
    instruction = ("\nSubmit submit_review_result exactly once with ALL THREE fields: verdict, summary, findings. "
        "If no concrete defect remains in the CURRENT candidate, verdict must be approved and findings must be []. "
        "Findings describe only unresolved current bugs: never praise, an old bug already fixed, or an explanation of the diff. "
        "For a supported current defect use changes_requested and anchor each finding to a declared changed path.")
    return prompt if prompt.endswith(instruction) else prompt + instruction


def read_observations(value: object) -> tuple[dict[str, object], ...]:
    """Validate persisted evidence as data; reading it never executes scripts."""
    if not isinstance(value, list) or len(value) > MAX_PROBES:
        raise ValueError("invalid reproduction evidence")
    result: list[dict[str, object]] = []
    for row in value:
        if not isinstance(row, dict) or row.get("semantic_admission") != "unverified":
            raise ValueError("invalid reproduction admission")
        if row.get("observation_kind") not in {"passed", "assertion_failed", "execution_error", "timeout", "invalid_proposal"}:
            raise ValueError("invalid reproduction observation")
        for key, limit in (("script", MAX_SCRIPT_CHARS), ("hypothesis", 1_000), ("requirement_quote", 1_000),
                           ("output", 24_000), ("command", 200), ("candidate_sha256", 64)):
            field = row.get(key, "")
            if not isinstance(field, str) or len(field) > limit:
                raise ValueError("invalid reproduction text")
        if row.get("exit_code") is not None and type(row["exit_code"]) is not int:
            raise ValueError("invalid reproduction exit code")
        if type(row.get("tests_run", 0)) is not int or row.get("tests_run", 0) < 0:
            raise ValueError("invalid reproduction count")
        result.append(dict(row))
    return tuple(result)
