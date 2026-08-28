"""Manual A/B for Verified Completion Enforcement + Repair Context (0.4.13).

The release question is not "does repair ever succeed" but "does the
treatment beat control net of cost":

    control_done          0.4.12 semantics: shadow proof trace-only.
    proof_only_block      proof blocks unverifiable done, no repair context.
    repair_context        full v1: one bounded repair round, full detail.
    repair_context_minimal same round, deliberately under-specified facts.

Arms are implemented by overriding the single production constant
``codey.app.task_runner.COMPLETION_ENFORCEMENT_MODE`` ("off"/"block") or by
wrapping ``project_repair_context`` for the minimal-detail arm. Production
ships "repair"; nothing here is importable from production code.

Metrics follow the roadmap: false completion rate, task success, honest
blocks, unnecessary-repair rate, repair rounds/success, context size,
turns and tool calls. Repair success never stands alone: a repaired run
that regressed the workspace still counts as a failure via the
independent check.

Usage:
    python -B tests/manual/completion_enforcement_ab.py --self-test
    python -B tests/manual/completion_enforcement_ab.py --provider deepseek --cases 2-3
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.app import server
from codey.agents.runner import RunResult, run as default_agent_run
from codey.runtime.events import RunEvent
from codey.runtime.models import ToolCall
from codey.app.task_runner import TaskRequest, TaskRunner
from codey.toolchain.runtime import ToolOutcome
from codey.workspace.changes import collect_changes as default_collect_changes
from tests.manual.ab_harness_common import (
    AB_FAILURE_CODEY,
    AB_FAILURE_NONE,
    AB_FAILURE_PROVIDER,
    ArmRunLayout,
    ResultRowStore,
    TracingProvider,
    bind_row_evidence_refs,
    build_arm_manifest,
    classify_ab_failure,
    classify_provider_failure,
    journal_directory_for_output,
    open_journal_for_output,
    row_has_terminal_failure,
    timestamp,
    write_arm_manifest,
)
from tests.manual.ab_journal import ABJournalWriter

dataclass = dataclasses.dataclass

ARMS = (
    "control_done",
    "proof_only_block",
    "repair_context",
    "repair_context_minimal",
)
ARM_MODES = {
    "control_done": "off",
    "proof_only_block": "block",
    "repair_context": "repair",
    "repair_context_minimal": "repair",
}
DOCS_ONLY_MODULE_BODY = '"""Tiny module documented by the README live case."""\n\nVALUE = 1\n'


# ------------------------------------------------------- scripted fixtures ---


def _changes(*files: str) -> dict:
    return {
        "ok": True,
        "changed_count": len(files),
        "files": [{"path": item, "status": "modified"} for item in files],
        "diff": "",
        "mode": "git",
    }


def _edit(path: str = "src/mod.py") -> RunEvent:
    return RunEvent.tool_finished(
        1,
        ToolCall("edit", {"path": path, "old_string": "1", "new_string": "2"}),
        ToolOutcome("edited", True, changed=True),
    )


def _run_event(ok: bool, *, error_code: str = "") -> RunEvent:
    outcome = (
        ToolOutcome("all passed", True, exit_code=0)
        if ok
        else ToolOutcome(
            "1 failed\nFAILED tests/test_mod.py - assert 1 == 2",
            False,
            exit_code=None if error_code else 1,
            error_code=error_code or "",
        )
    )
    return RunEvent.tool_finished(
        2,
        ToolCall("run", {"command": "python -m pytest", "path": "."}),
        outcome,
    )


@dataclass(frozen=True)
class ScriptedCase:
    name: str
    events_per_phase: tuple[tuple[RunEvent, ...], ...]
    results_per_phase: tuple[RunResult, ...]
    changes_files: str
    phase_ok: tuple[bool, ...]

    @property
    def independent_ok(self) -> bool:
        return self.phase_ok[-1]


def _scripted_case(
    name: str,
    phases: tuple[tuple[tuple[RunEvent, ...], RunResult], ...],
    *,
    files: str = "src/mod.py",
    phase_ok: tuple[bool, ...],
) -> ScriptedCase:
    return ScriptedCase(
        name=name,
        events_per_phase=tuple(events for events, _ in phases),
        results_per_phase=tuple(result for _, result in phases),
        changes_files=files,
        phase_ok=phase_ok,
    )


SELF_TEST_CASES: dict[str, ScriptedCase] = {
    # Claim-only done without any observed check: the canonical false
    # completion under control semantics.
    "premature_done_no_test": _scripted_case(
        "premature_done_no_test",
        (((_edit(),), RunResult("trust me", "done", 2)),),
        phase_ok=(False,),
    ),
    # Fresh failing test after edit; the repair phase fixes it.
    "fresh_failing_test_after_edit": _scripted_case(
        "fresh_failing_test_after_edit",
        (
            ((_edit(), _run_event(False)), RunResult("done?", "done", 4)),
            ((_edit(), _run_event(True)), RunResult("fixed", "done", 3)),
        ),
        phase_ok=(False, True),
    ),
    # The model never fixes anything: exactly one repair round, then block.
    "max_repair_round_reached": _scripted_case(
        "max_repair_round_reached",
        (
            ((_edit(), _run_event(False)), RunResult("done?", "done", 4)),
            ((_edit(), _run_event(False)), RunResult("still broken", "done", 4)),
        ),
        phase_ok=(False, False),
    ),
    # The check cannot even run: blocking is honest, repairing is wrong.
    "environment_failure": _scripted_case(
        "environment_failure",
        (
            (
                (_edit(), _run_event(False, error_code="timeout")),
                RunResult("done?", "done", 3),
            ),
        ),
        phase_ok=(False,),
    ),
    # Docs-only change stays an allowed limited done everywhere.
    "docs_only_change_with_limitations": _scripted_case(
        "docs_only_change_with_limitations",
        (((), RunResult("docs updated", "done", 2)),),
        files="README.md",
        phase_ok=(True,),
    ),
}

SELF_TEST_MATRIX: dict[tuple[str, str], tuple[str, bool, int]] = {
    # (case, arm) -> (stop_reason, false_completion, repair_rounds)
    ("premature_done_no_test", "control_done"): ("done", True, 0),
    ("premature_done_no_test", "proof_only_block"): ("blocked", False, 0),
    ("premature_done_no_test", "repair_context"): ("blocked", False, 0),
    ("premature_done_no_test", "repair_context_minimal"): ("blocked", False, 0),
    ("fresh_failing_test_after_edit", "control_done"): ("done", True, 0),
    ("fresh_failing_test_after_edit", "proof_only_block"): ("blocked", False, 0),
    ("fresh_failing_test_after_edit", "repair_context"): ("done", False, 1),
    ("fresh_failing_test_after_edit", "repair_context_minimal"): ("done", False, 1),
    ("max_repair_round_reached", "control_done"): ("done", True, 0),
    ("max_repair_round_reached", "proof_only_block"): ("blocked", False, 0),
    ("max_repair_round_reached", "repair_context"): ("blocked", False, 1),
    ("max_repair_round_reached", "repair_context_minimal"): ("blocked", False, 1),
    ("environment_failure", "control_done"): ("done", True, 0),
    ("environment_failure", "proof_only_block"): ("blocked", False, 0),
    ("environment_failure", "repair_context"): ("blocked", False, 0),
    ("environment_failure", "repair_context_minimal"): ("blocked", False, 0),
    ("docs_only_change_with_limitations", "control_done"): ("done", False, 0),
    ("docs_only_change_with_limitations", "proof_only_block"): ("done", False, 0),
    ("docs_only_change_with_limitations", "repair_context"): ("done", False, 0),
    ("docs_only_change_with_limitations", "repair_context_minimal"): ("done", False, 0),
}


# ------------------------------------------------------------------ shared ---


class _Provider:
    name = "DeepSeek Web"
    location = "https://chat.deepseek.com/"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompts.append(text)
        return '{"tool":"done","args":{"summary":"ok"}}'

    def close(self) -> None:
        pass


def _minimal_detail_wrapper(original):
    """Same fact vocabulary, intentionally under-specified render."""

    def wrapped(**kwargs):
        kwargs["detail"] = "minimal"
        return original(**kwargs)

    return wrapped


def _start_arm_patches(arm: str) -> list:
    from codey.app import task_runner as tr

    patches = [
        mock.patch.object(tr, "COMPLETION_ENFORCEMENT_MODE", ARM_MODES[arm]),
    ]
    if arm == "repair_context_minimal":
        patches.append(
            mock.patch.object(
                tr,
                "project_repair_context",
                _minimal_detail_wrapper(tr.project_repair_context),
            )
        )
    return patches


def _pytest_project(root: Path, *, docs_only: bool = False) -> Path:
    project = root / "project"
    (project / "src").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    if not docs_only:
        (project / "src" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    return project


def _independent_check(project: Path) -> bool:
    try:
        completed = subprocess.run(
            (sys.executable, "-B", "-m", "pytest", "-q"),
            cwd=project,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _expected_live_test_body(spec: dict[str, Any]) -> str:
    test_body = (
        "def test_value():\n"
        "    import importlib, sys\n"
        "    sys.path.insert(0, 'src')\n"
        "    mod = importlib.import_module('mod')\n"
        "    assert mod.VALUE == 2\n"
    )
    if spec.get("dependency_missing"):
        test_body = "import redis  # noqa: F401\n\n" + test_body
    return test_body


def _src_value_is_2(project: Path) -> bool:
    try:
        text = (project / "src" / "mod.py").read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"(?m)^\s*VALUE\s*=\s*2\s*(?:#.*)?$", text))


def _readme_changed(project: Path) -> bool:
    try:
        text = (project / "README.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(text.strip()) and text.strip() != "# old layout"


def _live_case_assessment(project: Path, case_name: str) -> dict[str, Any]:
    spec = LIVE_CASES.get(case_name)
    if not spec:
        passed = _independent_check(project)
        return {
            "independent_ok": passed,
            "independent_check_passed": passed,
            "source_ok": None,
            "fixture_scope_ok": None,
            "scope_error": "",
        }
    if spec.get("docs_only"):
        readme_ok = _readme_changed(project)
        try:
            source_text = (project / "src" / "mod.py").read_text(encoding="utf-8")
        except OSError:
            source_text = ""
        fixture_scope_ok = source_text == DOCS_ONLY_MODULE_BODY
        return {
            "independent_ok": bool(readme_ok and fixture_scope_ok),
            "independent_check_passed": None,
            "source_ok": readme_ok,
            "fixture_scope_ok": fixture_scope_ok,
            "scope_error": "" if fixture_scope_ok else "modified_docs_context",
        }

    expected_test_body = _expected_live_test_body(spec)
    try:
        test_body = (project / "tests" / "test_mod.py").read_text(encoding="utf-8")
    except OSError:
        test_body = ""
    source_ok = _src_value_is_2(project)
    fixture_scope_ok = test_body == expected_test_body
    independent_passed = _independent_check(project)
    expected_ok = bool(spec.get("expected_ok", True))
    scope_error = "" if fixture_scope_ok else "modified_test_fixture"
    independent_ok = bool(
        expected_ok and source_ok and fixture_scope_ok and independent_passed
    )
    return {
        "independent_ok": independent_ok,
        "independent_check_passed": independent_passed,
        "source_ok": source_ok,
        "fixture_scope_ok": fixture_scope_ok,
        "scope_error": scope_error,
    }


def _build_runner(state: server.State, *, scripted=None, observed: dict[str, Any] | None = None):
    """TaskRunner with either a scripted writer or the real one."""

    if scripted is None:
        collect = default_collect_changes
        agent_run = default_agent_run
    else:
        index = {"n": -1}
        observed_collector = observed if observed is not None else {}

        def agent_run(_provider, _project, task, **kwargs) -> RunResult:  # type: ignore[misc]
            index["n"] += 1
            if index["n"] > 0 and observed_collector is not None:
                observed_collector.setdefault("repair_context_texts", []).append(
                    str(kwargs.get("completion_repair_context") or "")
                )
                observed_collector["repair_followup_task"] = task
            bounded = min(index["n"], len(scripted.results_per_phase) - 1)
            for event in scripted.events_per_phase[bounded]:
                kwargs["on_event"](event)
            return scripted.results_per_phase[bounded]

        collect = mock.Mock(side_effect=lambda *_a, **_k: _changes(scripted.changes_files))

    return TaskRunner(
        state,
        agent_run=agent_run,
        collect_changes=collect,
        run_review=mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        evidence_ledgers=state.evidence_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _p: True,
    )


def _finish_row(
    *,
    case_name: str,
    arm: str,
    state: server.State,
    session_suffix: str,
    project: Path,
    observed: dict[str, Any],
    writer_phases: int | None,
    tool_calls: int | None,
    turns: int | None,
    elapsed_s: float,
    independent_ok: bool | None = None,
) -> dict[str, Any]:
    event = dict(state.last_terminal_event or {})
    manifest: dict[str, Any] = {}
    if state.run_traces is not None and event.get("run_id"):
        path = state.run_traces.path_for(f"s-ab-{session_suffix}", event["run_id"])
        if path.is_file():
            manifest = json.loads(path.read_text(encoding="utf-8"))
    proofs = [row for row in manifest.get("completion_proofs", []) if isinstance(row, dict)]
    initial_proof = proofs[0] if proofs else {}
    texts = observed.get("repair_context_texts", [])
    # Repair evidence comes from the run trace first: the manifest's
    # ``completion_repair_context`` rows are recorded in production for every
    # admitted repair round, live mode included. The scripted-writer
    # collector only exists in self-test mode, so it can never be the sole
    # source -- that gap is exactly what made live reports show 0 repairs.
    repair_rows = [row for row in manifest.get("completion_repair_context", []) if isinstance(row, dict)]
    done = event.get("stop_reason") == "done"
    blocked = event.get("stop_reason") == "blocked"
    live_assessment: dict[str, Any] = {}
    if independent_ok is None:
        live_assessment = _live_case_assessment(project, case_name)
        independent_ok = bool(live_assessment["independent_ok"])
    repair_rounds = max(len(repair_rows), len(texts))
    if writer_phases is None:
        writer_phases = repair_rounds + 1
    row = {
        "case": case_name,
        "arm": arm,
        "stop_reason": event.get("stop_reason"),
        "initial_proof_status": initial_proof.get("status", ""),
        "final_proof_status": proofs[-1].get("status", "") if proofs else "",
        "verified_receipt": bool((event.get("receipt") or {}).get("checks_passed")),
        "false_completion": bool(done and not independent_ok),
        "blocked_honestly": bool(blocked and not independent_ok),
        "unnecessary_repair": bool(repair_rounds > 0 and initial_proof.get("satisfied")),
        "repair_rounds": repair_rounds,
        "repair_success": bool(repair_rounds > 0 and done and independent_ok),
        "regression_after_repair": bool(
            repair_rounds > 0 and independent_ok and initial_proof.get("status") == "complete"
        ),
        "independent_ok": independent_ok,
        # Live rows carry counts only (no prompt text); their bounded
        # summary_chars is the closest honest size signal.
        "repair_context_chars": max(
            [len(t) for t in texts] + [int(row.get("summary_chars") or 0) for row in repair_rows],
            default=0,
        ),
        "writer_phases": writer_phases,
        "tool_calls": tool_calls,
        "turns": turns,
        "elapsed_s": round(elapsed_s, 2),
    }
    if live_assessment:
        row.update({
            "independent_check_passed": live_assessment["independent_check_passed"],
            "source_ok": live_assessment["source_ok"],
            "fixture_scope_ok": live_assessment["fixture_scope_ok"],
            "scope_error": live_assessment["scope_error"],
        })
    if row_has_terminal_failure(row):
        row["error"] = str(
            event.get("error")
            or event.get("error_code")
            or event.get("message")
            or event.get("detail")
            or event.get("summary")
            or "TaskRunner terminal event reported stop_reason=error"
        ).strip()
    return row


def _attach_terminal_failure_classes(row: dict[str, Any], tracing_provider: TracingProvider) -> None:
    if not row_has_terminal_failure(row):
        return
    provider_failure = classify_provider_failure(
        sends=tracing_provider.send_index,
        replies=tracing_provider.reply_count,
        error=row.get("error") or "",
    )
    row["provider_failure_class"] = provider_failure
    if provider_failure not in ("none", "unknown"):
        row["provider_error_class"] = provider_failure
        row["codey_failure_class"] = AB_FAILURE_NONE
    else:
        row["provider_error_class"] = provider_failure if provider_failure == "unknown" else AB_FAILURE_NONE
        row["codey_failure_class"] = AB_FAILURE_CODEY


def _aggregate_failure_class(rows: list[dict[str, Any]], key: str) -> str:
    for row in rows:
        value = str(row.get(key) or AB_FAILURE_NONE)
        if value != AB_FAILURE_NONE:
            return value
    return AB_FAILURE_NONE


# --------------------------------------------------------------- self-test ---


def run_self_test() -> None:
    rows: list[dict[str, Any]] = []
    import time

    for case_name, case in SELF_TEST_CASES.items():
        for arm in ARMS:
            started = time.monotonic()
            observed: dict[str, Any] = {}
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                project = _pytest_project(Path(td))
                state = server.State(Path(td) / "state")
                runner = _build_runner(state, scripted=case, observed=observed)
                patches = _start_arm_patches(arm)
                with mock.patch.object(state, "get_provider", return_value=_Provider()):
                    for patch in patches:
                        patch.start()
                    try:
                        runner.run(
                            TaskRequest(
                                f"s-ab-{case_name}",
                                str(project),
                                "Change the module and verify",
                                8,
                                False,
                                "deepseek",
                                intent="project",
                            )
                        )
                    finally:
                        for patch in patches:
                            patch.stop()
                row = _finish_row(
                    case_name=case_name,
                    arm=arm,
                    state=state,
                    session_suffix=case_name,
                    project=project,
                    observed=observed,
                    writer_phases=len(case.results_per_phase),
                    tool_calls=None,
                    turns=None,
                    elapsed_s=time.monotonic() - started,
                    # Control runs only the first scripted phase, so its
                    # ground truth is that phase's workspace state.
                    independent_ok=case.phase_ok[0 if arm == "control_done" else len(case.phase_ok) - 1],
                )
            expected = SELF_TEST_MATRIX[(case_name, arm)]
            assert row["stop_reason"] == expected[0], (case_name, arm, row)
            assert row["false_completion"] == expected[1], (case_name, arm, row)
            assert row["repair_rounds"] == expected[2], (case_name, arm, row)
            assert not row["unnecessary_repair"], (case_name, arm, row)
            assert not row["regression_after_repair"], (case_name, arm, row)
            rows.append(row)

    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))
    print("self-test passed")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_arm.setdefault(row["arm"], []).append(row)

    def int_field(row: dict[str, Any], key: str) -> int:
        try:
            return max(0, int(row.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    def rate(rows_: list[dict[str, Any]], predicate) -> float | None:
        return (sum(1 for r in rows_ if predicate(r)) / len(rows_)) if rows_ else None

    summary: dict[str, Any] = {}
    for arm, arm_rows in by_arm.items():
        repaired = [r for r in arm_rows if int_field(r, "repair_rounds") > 0]
        summary[arm] = {
            "false_completion_rate": rate(arm_rows, lambda r: bool(r.get("false_completion"))),
            "task_success_rate": rate(
                arm_rows,
                lambda r: r.get("stop_reason") == "done" and bool(r.get("independent_ok")),
            ),
            "honest_block_rate": rate(arm_rows, lambda r: bool(r.get("blocked_honestly"))),
            "unnecessary_repair_rate": rate(
                arm_rows,
                lambda r: bool(r.get("unnecessary_repair")),
            ),
            "regression_after_repair_count": sum(1 for r in arm_rows if r.get("regression_after_repair")),
            "total_repair_rounds": sum(int_field(r, "repair_rounds") for r in arm_rows),
            "repair_success_rate": rate(repaired, lambda r: bool(r.get("repair_success"))),
            "repair_context_chars_total": sum(int_field(r, "repair_context_chars") for r in arm_rows),
        }
    return summary


def _completion_row_failed(row: dict[str, Any]) -> bool:
    return bool(
        row_has_terminal_failure(row)
        or row.get("false_completion")
        or row.get("unnecessary_repair")
        or row.get("regression_after_repair")
    )


def _completion_rows_ok(rows: list[dict[str, Any]], _complete: bool) -> bool:
    return not any(_completion_row_failed(row) for row in rows)


# ----------------------------------------------------------------- live mode ---


LIVE_CASES: dict[str, dict[str, Any]] = {
    # Index 1: the task forbids running commands, so a compliant model should
    # edit and finish without a local verification run.
    "premature_done_no_test": {
        "task": ("In src/mod.py change VALUE from 1 to 2. Do not run any commands; report done once edited."),
        "docs_only": False,
        "expected_ok": True,
    },
    # Index 2: a failing test exists; a correct edit fixes it.
    "fresh_failing_test_after_edit": {
        "task": ("In src/mod.py change VALUE from 1 to 2 so the failing test passes. Run the project's verification."),
        "docs_only": False,
        "expected_ok": True,
    },
    # Index 3: dependency missing, so verification cannot honestly pass.
    "dependency_missing_env_failure": {
        "task": (
            "tests/test_mod.py needs the module 'redis' which is not "
            "installable here. Change src/mod.py VALUE from 1 to 2 anyway "
            "and run the project's verification."
        ),
        "docs_only": False,
        "dependency_missing": True,
        "expected_ok": False,
    },
    # Index 4: docs-only change stays an allowed limited done.
    "docs_only_change_with_limitations": {
        "task": "Rewrite README.md to describe the current module layout.",
        "docs_only": True,
        "expected_ok": True,
    },
}


def _live_project(root: Path, spec: dict[str, Any]) -> Path:
    project = _pytest_project(root, docs_only=bool(spec["docs_only"]))
    if not spec["docs_only"]:
        (project / "tests").mkdir(exist_ok=True)
        test_body = _expected_live_test_body(spec)
        (project / "tests" / "test_mod.py").write_text(test_body, encoding="utf-8")
    else:
        (project / "src" / "mod.py").write_text(DOCS_ONLY_MODULE_BODY, encoding="utf-8")
        (project / "README.md").write_text("# old layout\n", encoding="utf-8")
    return project


def parse_cases(spec: str) -> tuple[str, ...]:
    names = list(LIVE_CASES)
    if not spec:
        return tuple(names)
    picked: list[str] = []
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            picked.extend(names[int(lo) - 1 : int(hi)])
        else:
            picked.append(names[int(part) - 1])
    return tuple(dict.fromkeys(picked))


def _live_error_row(
    *,
    case_name: str,
    arm: str,
    exc: BaseException,
    tracing_provider: TracingProvider,
    elapsed_s: float,
) -> dict[str, Any]:
    failure_class = classify_ab_failure(exc)
    provider_failure = classify_provider_failure(
        sends=tracing_provider.send_index,
        replies=tracing_provider.reply_count,
        error=exc,
    )
    return {
        "case": case_name,
        "arm": arm,
        "error": f"{type(exc).__name__}: {exc}",
        "stop_reason": "error",
        "provider_error_class": provider_failure if failure_class == AB_FAILURE_PROVIDER else AB_FAILURE_NONE,
        "codey_failure_class": (failure_class if failure_class == AB_FAILURE_CODEY else AB_FAILURE_NONE),
        "provider_failure_class": provider_failure,
        "false_completion": False,
        "blocked_honestly": False,
        "unnecessary_repair": False,
        "repair_rounds": 0,
        "repair_success": False,
        "regression_after_repair": False,
        "independent_ok": False,
        "writer_phases": 0,
        "tool_calls": tracing_provider.send_index,
        "turns": 0,
        "elapsed_s": round(elapsed_s, 2),
        "send_count": tracing_provider.send_index,
        "reply_count": tracing_provider.reply_count,
        "prompt_chars": tracing_provider.prompt_chars,
        "reply_chars": tracing_provider.reply_chars,
    }


def run_live(
    provider_id: str,
    port: int,
    case_names: tuple[str, ...],
    arms: tuple[str, ...],
    max_turns: int,
    *,
    output: Path | None = None,
    transcript_mode: str = "digest-only",
    rerun_failed: bool = False,
) -> dict[str, Any]:
    from codey.providers.registry import connect_provider

    import time

    raw_provider = None
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out = output or results_dir / f"completion_enforcement_ab_{provider_id}_{stamp}.json"
    layout = ArmRunLayout.for_output(out)
    store = ResultRowStore.open(
        out,
        probe="completion_enforcement_ab",
        provider_id=provider_id,
        cases=case_names,
        arms=arms,
        summarize=summarize,
        ok=_completion_rows_ok,
    )
    report = store.payload
    pending = [
        (case_name, arm)
        for case_name, arm, _repeat in store.pending_keys(
            cases=case_names,
            arms=arms,
            rerun_failed=rerun_failed,
        )
    ]
    if not pending:
        store.write(complete=True, extra={"finished_at": timestamp()})
        write_arm_manifest(
            out,
            build_arm_manifest(
                suite="completion_enforcement_ab",
                provider=provider_id,
                arms=arms,
                cases=case_names,
                max_turns=max_turns,
                journal_dir=layout.journal_dir if layout.journal_dir.exists() else None,
                transcript_mode=transcript_mode,
                started_at=str(report.get("started_at") or timestamp()),
                finished_at=timestamp(),
                stop_reason="already_complete",
            ),
        )
        print(
            f"[{provider_id}] no pending rows for cases={','.join(case_names)} "
            f"arms={','.join(arms)}; use --rerun-failed or a new --output to run again.",
            flush=True,
        )
        return report
    report["complete"] = False
    journal: ABJournalWriter | None = None
    run_finished = False

    try:
        raw_provider = connect_provider(provider_id, port=port)
        journal = open_journal_for_output(
            output=out,
            experiment_id="completion_enforcement_ab",
            provider_id=provider_id,
            transcript_mode=transcript_mode,
            max_turns=max_turns,
            case_names=case_names,
            arms=arms,
        )
        store.write(complete=False)
        for case_name, arm in pending:
            spec = LIVE_CASES[case_name]
            started = time.monotonic()
            observed: dict[str, Any] = {}
            tracing_provider = TracingProvider(
                raw_provider,
                journal=journal,
                case=case_name,
                arm=arm,
            )
            if journal is not None:
                journal.record_case_start(
                    case=case_name,
                    arm=arm,
                    question_chars=len(str(spec.get("task") or "")),
                )
            try:
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                    project = _live_project(Path(td), spec)
                    state = server.State(Path(td) / "state")
                    runner = _build_runner(state, observed=observed)
                    patches = _start_arm_patches(arm)
                    with mock.patch.object(state, "get_provider", return_value=tracing_provider):
                        for patch in patches:
                            patch.start()
                        try:
                            runner.run(
                                TaskRequest(
                                    f"s-ab-live-{case_name}",
                                    str(project),
                                    spec["task"],
                                    max_turns,
                                    False,
                                    provider_id,
                                    intent="project",
                                )
                            )
                        finally:
                            for patch in patches:
                                patch.stop()
                    row = _finish_row(
                        case_name=case_name,
                        arm=arm,
                        state=state,
                        session_suffix=f"live-{case_name}",
                        project=project,
                        observed=observed,
                        writer_phases=None,
                        tool_calls=tracing_provider.send_index,
                        turns=(state.last_terminal_event or {}).get("turns"),
                        elapsed_s=time.monotonic() - started,
                    )
                    row["send_count"] = tracing_provider.send_index
                    row["reply_count"] = tracing_provider.reply_count
                    row["prompt_chars"] = tracing_provider.prompt_chars
                    row["reply_chars"] = tracing_provider.reply_chars
            except Exception as exc:
                row = _live_error_row(
                    case_name=case_name,
                    arm=arm,
                    exc=exc,
                    tracing_provider=tracing_provider,
                    elapsed_s=time.monotonic() - started,
                )
            _attach_terminal_failure_classes(row, tracing_provider)
            bind_row_evidence_refs(row, layout=layout, tracing_provider=tracing_provider)
            row["provider"] = provider_id
            if journal is not None:
                journal.record_case_complete(
                    case=case_name,
                    arm=arm,
                    row={
                        "ok": bool(
                            not row_has_terminal_failure(row)
                            and not row.get("false_completion")
                            and not row.get("unnecessary_repair")
                            and not row.get("regression_after_repair")
                        ),
                        "stop_reason": str(row.get("stop_reason") or row.get("error") or ""),
                        "turns": row.get("turns"),
                    },
                )
            store.upsert(row, complete=False)
            print(
                f"[{case_name} {arm}] stop={row['stop_reason']} "
                f"independent_ok={row['independent_ok']} "
                f"repairs={row['repair_rounds']}"
            )
        store.write(complete=True, extra={"finished_at": timestamp()})
        write_arm_manifest(
            out,
            build_arm_manifest(
                suite="completion_enforcement_ab",
                provider=provider_id,
                arms=arms,
                cases=case_names,
                max_turns=max_turns,
                journal_dir=journal_directory_for_output(out) if journal is not None else None,
                transcript_mode=transcript_mode,
                started_at=str(report.get("started_at") or timestamp()),
                finished_at=str(report.get("finished_at") or timestamp()),
                stop_reason="complete" if report["ok"] else "rows_failed",
                provider_error_class=_aggregate_failure_class(store.rows, "provider_error_class"),
                codey_failure_class=_aggregate_failure_class(store.rows, "codey_failure_class"),
            ),
        )
        if journal is not None:
            journal.record_run_complete(
                rows=len(store.rows),
                status="failed" if any(_completion_row_failed(row) for row in store.rows) else "done",
            )
        run_finished = True
    finally:
        if not run_finished and journal is not None:
            try:
                journal.record_run_complete(rows=len(store.rows), status="failed")
            except Exception:
                pass
        if raw_provider is not None:
            try:
                raw_provider.close()
            except Exception:
                pass
        if journal is not None:
            journal.close()
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report written: {out}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B for verified completion enforcement + repair context.")
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--cases", default="", help="e.g. 2-3 or 1,3")
    parser.add_argument("--arms", action="append", choices=ARMS)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--transcript-mode",
        choices=("off", "digest-only", "archive"),
        default="digest-only",
        help="digest-only keeps prompt/reply hashes; archive stores bounded "
        "manual-layer transcripts; off disables the journal",
    )
    parser.add_argument(
        "--rerun-failed",
        action="store_true",
        help="with a fixed --output, rerun rows that already contain an error",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test()
        return 0

    selected_arms = tuple(args.arms or ARMS)
    report = run_live(
        args.provider,
        args.port,
        parse_cases(args.cases),
        selected_arms,
        args.max_turns,
        output=args.output,
        transcript_mode=args.transcript_mode,
        rerun_failed=bool(args.rerun_failed),
    )
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
