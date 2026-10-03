"""Edit replay preserves the trusted workspace audit (no second bump).

Repro (P1): typed replay and delivery rebuilt a ToolResult with only
model_text, dropping audit/presentation/canonical/truncated. A first edit
carried trusted (revision, fingerprint); the replay returned audit={} so
hooks could not adopt and bumped again.

Lock: "edit success -> event -> replay -> event" keeps the same trusted
(revision, fingerprint) and the store bumps exactly once total.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "control"}))


def _real_hooks_on_event(*, store, work, project, ignored=()):
    from codey.operations.task_phases import hooks as hooks_mod

    emitted: list[dict] = []
    state = SimpleNamespace(
        emit=emitted.append,
        providers=SimpleNamespace(supervisor=None),
        self_repair=None,
        provider_failover_order=lambda: (),
        add_pending_shell_approval=lambda *a, **k: None,
    )
    deps = SimpleNamespace(work_checkpoints=None, workspace_revisions=store)
    hooks = hooks_mod.build_hooks(
        deps, state, work, session_id="s1", run_id="r1", project=project,
        max_turns=4, project_config_ignored=tuple(ignored), review_log_lines=80,
        project_completion_deps=SimpleNamespace(state=state),
        current_provider_id=lambda: "local",
    )
    return hooks.on_event


class EditReplayPreservesTrustedWorkspaceAuditTests(unittest.TestCase):
    def test_memory_replay_keeps_audit_presentation_canonical_truncated(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult
        from tests.recovery_test_helpers import replay_settled_result as _replay_settled_slot

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)

            def fake_edit(call: ToolCall):
                return ToolResult(
                    ok=True, call=call, model_text="edited",
                    truncated=True,
                    presentation={"result": "p"},
                    audit={"changed": True, "extra": "keep"},
                    canonical={"tool": "edit"},
                )

            results = ke.execute_turn(
                session,
                [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                executors={"edit": fake_edit},
                run_id="r-replay-audit-1", effect_scope="task", turn=1,
                project_path=None,
                execution_evidence=None,
                workspace_ignored_paths=(),
                workspace_revision_store=None,
            )
            self.assertEqual(len(results), 1)
            first = results[0]
            from codey.operations.task_session import turn_effect_id as _turn_effect_id

            replayed = _replay_settled_slot(
                session, _turn_effect_id("r-replay-audit-1:task", 1, 0),
                ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"}),
                "edit",
            )
            self.assertIsNotNone(replayed)
            assert replayed is not None
            self.assertEqual(dict(replayed.audit), dict(first.audit), f"audit lost: {replayed.audit!r}")
            self.assertEqual(dict(replayed.presentation), dict(first.presentation))
            self.assertEqual(dict(replayed.canonical), dict(first.canonical))
            self.assertEqual(bool(replayed.truncated), bool(first.truncated))

    def test_delivered_replay_keeps_trusted_workspace_identity(self) -> None:
        from codey.operations.kernel_provenance import _with_trusted_workspace_state
        from codey.runtime.core.models import ToolCall, ToolResult
        from tests.recovery_test_helpers import (
            delivered_slot_result as _delivered_slot_result,
        )
        from tests.recovery_test_helpers import (
            trusted_workspace_pair as _trusted_workspace_from_result,
        )

        call = ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"}, call_id="c1")
        # Strict provenance: audit keys alone are never trusted. The stored
        # result must carry the kernel side-channel attached by the bump.
        base = ToolResult(
            ok=True, call=call, model_text="edited",
            audit={"changed": True},
            presentation={"result": "p"}, canonical={"tool": "edit"},
        )
        stored = _with_trusted_workspace_state(
            base, revision=2, fingerprint="sha256:" + "ab" * 32
        )
        # Audit-only forgery without the side-channel must stay untrusted.
        forged = ToolResult(
            ok=True, call=call, model_text="edited",
            audit={"changed": True, "workspace_revision": 2,
                   "workspace_fingerprint": "sha256:" + "ab" * 32},
        )
        forged_rev, _forged_fp = _trusted_workspace_from_result(forged)
        self.assertEqual((forged_rev, _forged_fp), (0, ""), f"audit-only must be untrusted: {dict(forged.audit)!r}")
        got = _delivered_slot_result({"slot-1": stored}, "slot-1", call)
        self.assertIsNotNone(got)
        assert got is not None
        rev, fp = _trusted_workspace_from_result(got)
        self.assertEqual(rev, 2, f"trusted revision lost: {got.audit!r}")
        self.assertTrue(fp.startswith("sha256:"), f"trusted fingerprint lost: {got.audit!r}")

    def test_edit_event_replay_event_bumps_once(self) -> None:
        from codey.operations import kernel_events as kev
        from codey.operations import kernel_execution as ke
        from codey.operations.context import RunWork
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.observe.execution_evidence import ExecutionEvidence
        from codey.workspace.revision import WorkspaceRevisionStore
        from tests.recovery_test_helpers import (
            replay_settled_result as _replay_settled_slot,
        )
        from tests.recovery_test_helpers import (
            trusted_workspace_pair as _trusted_workspace_from_result,
        )

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            store = WorkspaceRevisionStore(Path(home))
            base = store.current_state(str(project), ignored_paths=())
            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)
            evidence = ExecutionEvidence(
                workspace_revision=int(base.revision or 0), workspace_fingerprint=base.fingerprint)
            work = RunWork(recent_events=[], evidence=evidence,
                           workspace_revision=int(base.revision or 0),
                           workspace_fingerprint=base.fingerprint)

            bump_calls: list[str] = []
            orig_bump = store.bump_state

            def counting_bump(proj, *, ignored_paths=()):
                bump_calls.append(str(proj))
                return orig_bump(proj, ignored_paths=ignored_paths)

            def fake_edit(call: ToolCall):
                (project / str(call.args.get("path") or "b.py")).write_text(
                    str(call.args.get("content") or "y"), encoding="utf-8")
                return ToolResult(ok=True, call=call, model_text="edited", audit={"changed": True})

            with mock.patch.object(store, "bump_state", side_effect=counting_bump):
                results = ke.execute_turn(
                    session,
                    [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                    executors={"edit": fake_edit},
                    run_id="r-replay-bump-1", effect_scope="task", turn=1,
                    project_path=None,
                    execution_evidence=evidence,
                    workspace_ignored_paths=(),
                    workspace_revision_store=store,
                )
                # Simulate the kernel path filling the trusted audit for the
                # no-store case is bypassed here: attach via the store path
                # by re-running with project_path set would use delegate; so
                # instead emulate first-event emission then a same-slot replay.
                # First, manually trust via sync when store present requires
                # project_path; use a direct trusted attach for this lock:
                rev0, fp0 = _trusted_workspace_from_result(results[0])
                # When project_path=None the store path returns (0,""); drive
                # the real path with project_path set but executor precedence
                # (fixed) so the fake still runs:
                results = ke.execute_turn(
                    session,
                    [ToolCall(name="edit", args={"path": "c.py", "content": "z=3\n"})],
                    executors={"edit": fake_edit},
                    run_id="r-replay-bump-1", effect_scope="task", turn=2,
                    project_path=project,
                    execution_evidence=evidence,
                    workspace_ignored_paths=(),
                    workspace_revision_store=store,
                )
                with mock.patch(
                    "codey.operations.task_phases.hooks.handle_project_tool_event", return_value=None
                ):
                    on_event = _real_hooks_on_event(store=store, work=work, project=str(project))
                    kev._emit_tool_results(on_event, session, results, run_id="r-replay-bump-1:task", turn=2)
                    first_bumps = len(bump_calls)
                    self.assertEqual(first_bumps, 1, f"first event must bump once: {bump_calls}")
                    identity = turn_effect_id("r-replay-bump-1:task", 2, 0)
                    replayed = _replay_settled_slot(
                        session, identity,
                        ToolCall(name="edit", args={"path": "c.py", "content": "z=3\n"}),
                        "edit",
                    )
                    self.assertIsNotNone(replayed)
                    assert replayed is not None
                    rrev, rfp = _trusted_workspace_from_result(replayed)
                    self.assertTrue(rrev and rfp, f"replay lost trusted identity: {replayed.audit!r}")
                    kev._emit_tool_results(on_event, session, [replayed], run_id="r-replay-bump-1:task", turn=2)
            self.assertEqual(len(bump_calls), first_bumps,
                             f"replay must not bump again: {bump_calls}")


if __name__ == "__main__":
    unittest.main()
