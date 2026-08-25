"""Manual A/B for Verified Completion Enforcement + Repair Context (0.4.13).

The release question is not "does repair ever succeed" but "does the
treatment beat control net of cost":

    control_done          0.4.12 semantics: shadow proof trace-only.
    proof_only_block      proof blocks unverifiable done, no repair context.
    repair_context        full v1: one bounded repair round, full detail.
    repair_context_minimal same round, deliberately under-specified facts.

Arms are implemented by overriding the single production constant
``codey.task_runner.COMPLETION_ENFORCEMENT_MODE`` ("off"/"block") or by
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
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import server
from codey.agent import RunResult
from codey.events import RunEvent
from codey.models import ToolCall
from codey.task_runner import TaskRequest, TaskRunner
from codey.tool_runtime import ToolOutcome

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
    from codey import task_runner as tr

    patches = [
        mock.patch.object(tr, "COMPLETION_ENFORCEMENT_MODE", ARM_MODES[arm]),
    ]
    if arm == "repair_context_minimal":
        patches.append(mock.patch.object(
            tr,
            "project_repair_context",
            _minimal_detail_wrapper(tr.project_repair_context),
        ))
    return patches


def _pytest_project(root: Path, *, docs_only: bool = False) -> Path:
    project = root / "project"
    (project / "src").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
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


def _build_runner(state: server.State, *, scripted=None, observed: dict[str, Any] | None = None):
    """TaskRunner with either a scripted writer or the real one."""

    if scripted is None:
        collect = None
        agent_run = None
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
    writer_phases: int,
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
    done = event.get("stop_reason") == "done"
    blocked = event.get("stop_reason") == "blocked"
    if independent_ok is None:
        independent_ok = _independent_check(project)
    repair_rounds = len(texts)
    return {
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
        "repair_success": bool(
            repair_rounds > 0 and done and independent_ok
        ),
        "regression_after_repair": bool(
            repair_rounds > 0 and independent_ok and initial_proof.get("status") == "complete"
        ),
        "independent_ok": independent_ok,
        "repair_context_chars": max((len(t) for t in texts), default=0),
        "writer_phases": writer_phases,
        "tool_calls": tool_calls,
        "turns": turns,
        "elapsed_s": round(elapsed_s, 2),
    }


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
                        runner.run(TaskRequest(
                            f"s-ab-{case_name}",
                            str(project),
                            "Change the module and verify",
                            8,
                            False,
                            "deepseek",
                            intent="project",
                        ))
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
                    independent_ok=case.phase_ok[
                        0 if arm == "control_done" else len(case.phase_ok) - 1
                    ],
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

    def rate(rows_: list[dict[str, Any]], predicate) -> float | None:
        return (sum(1 for r in rows_ if predicate(r)) / len(rows_)) if rows_ else None

    summary: dict[str, Any] = {}
    for arm, arm_rows in by_arm.items():
        repaired = [r for r in arm_rows if r["repair_rounds"] > 0]
        summary[arm] = {
            "false_completion_rate": rate(arm_rows, lambda r: r["false_completion"]),
            "task_success_rate": rate(
                arm_rows,
                lambda r: r["stop_reason"] == "done" and r["independent_ok"],
            ),
            "honest_block_rate": rate(arm_rows, lambda r: r["blocked_honestly"]),
            "unnecessary_repair_rate": rate(arm_rows, lambda r: r["unnecessary_repair"]),
            "regression_after_repair_count": sum(
                1 for r in arm_rows if r["regression_after_repair"]
            ),
            "total_repair_rounds": sum(r["repair_rounds"] for r in arm_rows),
            "repair_success_rate": rate(repaired, lambda r: r["repair_success"]),
            "repair_context_chars_total": sum(
                r["repair_context_chars"] for r in arm_rows
            ),
        }
    return summary


# ----------------------------------------------------------------- live mode ---


LIVE_CASES: dict[str, dict[str, Any]] = {
    # Index 1: the task forbids running commands, so a compliant model can
    # only produce a claim-only done -- control records a false completion,
    # enforcement blocks it.
    "premature_done_no_test": {
        "task": (
            "In src/mod.py change VALUE from 1 to 2. Do not run any "
            "commands; report done once edited."
        ),
        "docs_only": False,
        "expected_ok": True,
    },
    # Index 2: a failing test exists; a correct edit fixes it.
    "fresh_failing_test_after_edit": {
        "task": (
            "In src/mod.py change VALUE from 1 to 2 so the failing test "
            "passes. Run the project's verification."
        ),
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
        "expected_ok": False,
    },
    # Index 5: docs-only change stays an allowed limited done.
    "docs_only_change_with_limitations": {
        "task": "Rewrite README.md to describe the new module layout.",
        "docs_only": True,
        "expected_ok": True,
    },
}


def _live_project(root: Path, spec: dict[str, Any]) -> Path:
    project = _pytest_project(root, docs_only=bool(spec["docs_only"]))
    if not spec["docs_only"]:
        (project / "tests").mkdir(exist_ok=True)
        (project / "tests" / "test_mod.py").write_text(
            "import redis  # noqa: F401\n\n"
            "def test_value():\n"
            "    import importlib, sys\n"
            "    sys.path.insert(0, 'src')\n"
            "    mod = importlib.import_module('mod')\n"
            "    assert mod.VALUE == 2\n",
            encoding="utf-8",
        )
    else:
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


def run_live(provider_id: str, port: int, case_names: tuple[str, ...], arms: tuple[str, ...], max_turns: int) -> dict[str, Any]:
    from codey.providers.registry import connect_provider

    rows: list[dict[str, Any]] = []
    provider = None
    import time

    try:
        provider = connect_provider(provider_id, port=port)
        for case_name in case_names:
            spec = LIVE_CASES[case_name]
            for arm in arms:
                started = time.monotonic()
                observed: dict[str, Any] = {}
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                    project = _live_project(Path(td), spec)
                    state = server.State(Path(td) / "state")
                    runner = _build_runner(state, observed=observed)
                    patches = _start_arm_patches(arm)
                    with mock.patch.object(state, "get_provider", return_value=provider):
                        for patch in patches:
                            patch.start()
                        try:
                            runner.run(TaskRequest(
                                f"s-ab-live-{case_name}",
                                str(project),
                                spec["task"],
                                max_turns,
                                False,
                                provider_id,
                                intent="project",
                            ))
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
                        writer_phases=max(1, observed.get("repair_rounds", 0) + 1),
                        tool_calls=None,
                        turns=None,
                        elapsed_s=time.monotonic() - started,
                    )
                rows.append(row)
                print(
                    f"[{case_name} {arm}] stop={row['stop_reason']} "
                    f"independent_ok={row['independent_ok']} "
                    f"repairs={row['repair_rounds']}"
                )
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass
    report = {"provider": provider_id, "summary": summarize(rows), "results": rows}
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    out = results_dir / f"completion_enforcement_ab_{provider_id}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report written: {out}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A/B for verified completion enforcement + repair context."
    )
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--cases", default="", help="e.g. 2-3 or 1,3")
    parser.add_argument("--arms", action="append", choices=ARMS)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test()
        return 0

    selected_arms = tuple(args.arms or ARMS)
    run_live(
        args.provider,
        args.port,
        parse_cases(args.cases),
        selected_arms,
        args.max_turns,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
