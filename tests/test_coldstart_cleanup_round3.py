"""Cold-start cleanup round3 locks (red-first).

Covers the four items requested as high-certainty cold-start residue:

1. Run Trace rejects missing/invalid schema versions (topic continuity +
   completion repair) before touching the digest dedupe key; prompt-surface
   path drops unreachable empty checks and ``or 1`` backfills.
2. ``agent`` CLI reads required fields directly (no getattr defaults).
3. Completion proof reads required workspace identity directly.
4. Duplicate projection code is merged (single helper / single call).

These tests assert the CLEANED state, so they fail on the pre-cleanup code
and pass after the cleanup.
"""
from __future__ import annotations

import argparse
import inspect as std_inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _topic_payload() -> dict[str, object]:
    from codey.research.topic_continuity import project_topic_continuity

    projection = project_topic_continuity(
        interest_hints=[{"ref": "r1", "question": "Lead?"}],
    )
    assert projection.admitted
    return dict(projection.to_payload())


def _repair_payload() -> dict[str, object]:
    from codey.completion.repair_context import project_repair_context

    projection = project_repair_context(
        proof={
            "status": "failed",
            "proof_id": "completion_proof:" + "a" * 16,
            "contract_id": "completion_contract:" + "b" * 16,
            "reason_codes": ["relevant_verification_failed"],
            "checks": [
                {
                    "check_id": "relevant_verification",
                    "status": "fail",
                    "reason_code": "relevant_verification_failed",
                }
            ],
        },
        failure_class="product_failure",
        decisive_checks=[{
            "command": "pytest -q",
            "cwd": ".",
            "exit_code": 1,
            "result_summary": "FAILED tests/test_x.py - assert 1 == 2",
        }],
        changed_files=["src/foo.py"],
    )
    assert projection.admitted
    return dict(projection.to_payload())


def _open(store, run_id: str, session_id: str = "s-r3"):
    return store.open(
        run_id=run_id,
        session_id=session_id,
        project=None,
        mode_initial="agent",
        provider_initial="deepseek",
    )


def _manifest(store, session_id: str, run_id: str) -> dict:
    return json.loads(
        store.path_for(session_id, run_id).read_text(encoding="utf-8")
    )


def _epoch(seed: str) -> str:
    from codey.workspace.context_epoch import context_epoch_id

    return context_epoch_id(seed)


class TopicContinuitySchemaStrictTests(unittest.TestCase):
    def _rejected(self, mutated: dict[str, object]) -> tuple[int, int]:
        """Return (rows_after_bad, rows_after_good_retry_same_digest)."""
        from codey.runs.trace import RunTraceStore

        epoch = _epoch("round3 topic continuity outbound")
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = _open(store, "run-tc-strict")
            recorder.record_research_topic_continuity(mutated, epoch_id=epoch)
            recorder.flush()
            bad_rows = _manifest(store, "s-r3", "run-tc-strict")[
                "research_topic_continuity"
            ]
            # Retry with the valid payload carrying the SAME digest: the
            # rejected call must not have occupied the dedupe key.
            good = _topic_payload()
            # Both payloads come from the same deterministic projection;
            # they must already share a digest. Fail instead of rewriting
            # so an unexpected sample change cannot be masked.
            self.assertEqual(
                good.get("digest"),
                mutated.get("digest"),
                "topic continuity sample digest changed unexpectedly",
            )
            recorder.record_research_topic_continuity(good, epoch_id=epoch)
            recorder.flush()
            manifest = _manifest(store, "s-r3", "run-tc-strict")
        return len(bad_rows), len(manifest["research_topic_continuity"])

    def test_missing_schema_version_rejected_without_dedupe(self) -> None:
        payload = _topic_payload()
        digest = payload["digest"]
        mutated = dict(payload)
        del mutated["schema_version"]
        bad, after = self._rejected(mutated)
        self.assertEqual(bad, 0, "missing schema_version must write no row")
        self.assertEqual(after, 1, "rejection must not occupy digest key")
        self.assertEqual(digest, payload["digest"])

    def test_zero_schema_version_rejected_without_dedupe(self) -> None:
        payload = _topic_payload()
        mutated = dict(payload, schema_version=0)
        bad, after = self._rejected(mutated)
        self.assertEqual(bad, 0)
        self.assertEqual(after, 1)

    def test_bool_schema_version_rejected_without_dedupe(self) -> None:
        payload = _topic_payload()
        mutated = dict(payload, schema_version=True)
        bad, after = self._rejected(mutated)
        self.assertEqual(bad, 0, "bool True must not pass as int 1")
        self.assertEqual(after, 1)

    def test_wrong_schema_version_rejected_without_dedupe(self) -> None:
        payload = _topic_payload()
        mutated = dict(payload, schema_version=2)
        bad, after = self._rejected(mutated)
        self.assertEqual(bad, 0)
        self.assertEqual(after, 1)

    def test_string_schema_version_rejected(self) -> None:
        from codey.runs.trace import RunTraceStore

        payload = _topic_payload()
        mutated = dict(payload, schema_version="1")
        epoch = _epoch("round3 topic string version")
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = _open(store, "run-tc-str")
            recorder.record_research_topic_continuity(mutated, epoch_id=epoch)
            recorder.flush()
            rows = _manifest(store, "s-r3", "run-tc-str")[
                "research_topic_continuity"
            ]
        self.assertEqual(rows, [])

    def test_valid_schema_version_still_admitted(self) -> None:
        from codey.runs.trace import RunTraceStore

        payload = _topic_payload()
        self.assertEqual(payload["schema_version"], 1)
        epoch = _epoch("round3 topic valid")
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = _open(store, "run-tc-valid")
            recorder.record_research_topic_continuity(payload, epoch_id=epoch)
            recorder.flush()
            rows = _manifest(store, "s-r3", "run-tc-valid")[
                "research_topic_continuity"
            ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema_version"], 1)
        self.assertEqual(rows[0]["digest"], payload["digest"])

    def test_no_or1_backfill_in_topic_path(self) -> None:
        import codey.runs.trace as trace_mod

        source = std_inspect.getsource(
            trace_mod.RunTraceRecorder.record_research_topic_continuity
        )
        self.assertNotIn("or 1", source)
        # strict check must be exact-int equality against the projection const
        self.assertIn("TOPIC_CONTINUITY_SCHEMA_VERSION", source)
        self.assertIn("type(", source)


class CompletionRepairSchemaStrictTests(unittest.TestCase):
    def _rejected(self, mutated: dict[str, object]) -> tuple[int, int]:
        from codey.runs.trace import RunTraceStore

        epoch = _epoch("round3 repair outbound")
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = _open(store, "run-repair-strict")
            recorder.record_completion_repair_context(mutated, epoch_id=epoch)
            recorder.flush()
            bad_rows = _manifest(store, "s-r3", "run-repair-strict")[
                "completion_repair_context"
            ]
            good = _repair_payload()
            self.assertEqual(
                good.get("digest"),
                mutated.get("digest"),
                "repair context sample digest changed unexpectedly",
            )
            recorder.record_completion_repair_context(good, epoch_id=epoch)
            recorder.flush()
            manifest = _manifest(store, "s-r3", "run-repair-strict")
        return len(bad_rows), len(manifest["completion_repair_context"])

    def test_missing_schema_version_rejected_without_dedupe(self) -> None:
        payload = _repair_payload()
        mutated = dict(payload)
        del mutated["schema_version"]
        bad, after = self._rejected(mutated)
        self.assertEqual(bad, 0)
        self.assertEqual(after, 1)

    def test_zero_schema_version_rejected_without_dedupe(self) -> None:
        mutated = dict(_repair_payload(), schema_version=0)
        bad, after = self._rejected(mutated)
        self.assertEqual(bad, 0)
        self.assertEqual(after, 1)

    def test_bool_schema_version_rejected_without_dedupe(self) -> None:
        mutated = dict(_repair_payload(), schema_version=True)
        bad, after = self._rejected(mutated)
        self.assertEqual(bad, 0)
        self.assertEqual(after, 1)

    def test_wrong_schema_version_rejected_without_dedupe(self) -> None:
        mutated = dict(_repair_payload(), schema_version=2)
        bad, after = self._rejected(mutated)
        self.assertEqual(bad, 0)
        self.assertEqual(after, 1)

    def test_valid_schema_version_still_admitted(self) -> None:
        from codey.runs.trace import RunTraceStore

        payload = _repair_payload()
        self.assertEqual(payload["schema_version"], 1)
        epoch = _epoch("round3 repair valid")
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = _open(store, "run-repair-valid")
            recorder.record_completion_repair_context(payload, epoch_id=epoch)
            recorder.flush()
            rows = _manifest(store, "s-r3", "run-repair-valid")[
                "completion_repair_context"
            ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema_version"], 1)

    def test_no_or1_backfill_in_repair_path(self) -> None:
        import codey.runs.trace as trace_mod

        source = std_inspect.getsource(
            trace_mod.RunTraceRecorder.record_completion_repair_context
        )
        self.assertNotIn("or 1", source)
        self.assertIn("COMPLETION_REPAIR_SCHEMA_VERSION", source)
        self.assertIn("type(", source)


class PromptSurfaceStrictTests(unittest.TestCase):
    def test_serialization_preserves_zero_schema_version(self) -> None:
        from codey.runs.trace import PromptSurfaceTrace

        row = PromptSurfaceTrace(
            surface_id="prompt_surface:" + "a" * 16,
            phase="writer",
            prompt_digest="sha256:" + "b" * 64,
            prompt_chars=10,
            epoch_id="ctx_epoch:" + "c" * 16,
            send_ref="effect_1",
            schema_version=0,
        )
        self.assertEqual(row.to_payload()["schema_version"], 0)

    def test_append_uses_validated_version_directly(self) -> None:
        import codey.runs.trace as trace_mod

        source = std_inspect.getsource(
            trace_mod.RunTraceRecorder._append_prompt_surface
        )
        self.assertNotIn("or 1", source)
        self.assertIn('payload["schema_version"]', source)

    def test_unreachable_empty_checks_removed_but_dedupe_kept(self) -> None:
        import codey.runs.trace as trace_mod

        source = std_inspect.getsource(
            trace_mod.RunTraceRecorder._append_prompt_surface
        )
        self.assertNotIn("if not surface_id", source)
        self.assertNotIn("if not send_ref", source)
        self.assertIn("_prompt_surface_keys", source)

    def test_to_payload_writes_schema_version_directly(self) -> None:
        import codey.runs.trace as trace_mod

        source = std_inspect.getsource(trace_mod.PromptSurfaceTrace.to_payload)
        self.assertNotIn("or 1", source)


class AgentCliStrictTests(unittest.TestCase):
    def test_cmd_agent_reads_required_fields_directly(self) -> None:
        import codey.app.cli as cli

        source = std_inspect.getsource(cli.cmd_agent)
        for field in ("args.json", "args.state_home", "args.max_turns",
                      "args.readonly", "args.auto"):
            with self.subTest(field=field):
                self.assertIn(field, source)
        for legacy in ('getattr(args, "json"', 'getattr(args, "state_home"',
                       'getattr(args, "max_turns"', 'getattr(args, "readonly"',
                       'getattr(args, "auto"'):
            with self.subTest(legacy=legacy):
                self.assertNotIn(legacy, source)

    def test_cmd_agent_missing_field_raises_contract_error(self) -> None:
        import codey.app.cli as cli

        with tempfile.TemporaryDirectory() as td:
            # Deliberately missing json/state_home/max_turns/readonly/auto.
            args = argparse.Namespace(
                task=["fix"],
                project=td,
                provider="deepseek",
                port=9222,
            )
            with mock.patch(
                "codey.app.headless_runner.run_headless",
                return_value=mock.Mock(exit_code=0),
            ), self.assertRaises(AttributeError):
                cli.cmd_agent(args)

    def test_cmd_agent_keeps_option_defaults(self) -> None:
        import io

        import codey.app.cli as cli
        from codey.agents.request import DEFAULT_MAX_TURNS
        from codey.storage.local_store import DEFAULT_STATE_HOME

        seen: dict[str, object] = {}

        def fake_headless(request, *, emit_jsonl):
            seen["max_turns"] = request.max_turns
            seen["state_home"] = request.state_home
            seen["intent"] = request.intent
            from codey.app.headless_runner import HeadlessResult

            return HeadlessResult(0, "run-1", "session-1", "done")

        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(
                task=["fix"],
                project=td,
                provider="deepseek",
                port=9222,
                json=True,
                state_home="",
                max_turns=None,
                readonly=False,
                auto=False,
            )
            with (
                mock.patch(
                    "codey.app.headless_runner.run_headless",
                    side_effect=fake_headless,
                ),
                mock.patch("sys.stdout", io.StringIO()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                exit_code = cli.cmd_agent(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(seen["max_turns"], DEFAULT_MAX_TURNS)
        self.assertEqual(Path(seen["state_home"]) if isinstance(seen["state_home"], (str, Path)) else seen["state_home"], DEFAULT_STATE_HOME)
        self.assertEqual(seen["intent"], "project")

    def test_agent_via_main_parser(self) -> None:
        import io

        import codey.app.cli as cli
        from codey.app.headless_runner import HeadlessResult

        with (
            tempfile.TemporaryDirectory() as td, mock.patch(
                "codey.app.headless_runner.run_headless",
                return_value=HeadlessResult(0, "run-1", "session-1", "done"),
            ) as run_headless,
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            code = cli.main([
                "agent", "--project", td, "--provider", "deepseek", "fix it",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(run_headless.call_count, 1)

    def test_ghost_getattr_kept(self) -> None:
        import codey.app.cli as cli

        source = std_inspect.getsource(cli)
        # ghost subcommands legitimately differ per subcommand; keep fallback.
        self.assertIn('getattr(args, "status"', source)


class CompletionEvidenceDirectTests(unittest.TestCase):
    def test_decision_reads_workspace_identity_directly(self) -> None:
        import codey.completion.decision as decision_mod

        source = std_inspect.getsource(decision_mod.build_completion_decision)
        self.assertIn("evidence.workspace_revision", source)
        self.assertIn("evidence.workspace_fingerprint", source)
        self.assertNotIn('getattr(evidence, "workspace_revision"', source)
        self.assertNotIn('getattr(evidence, "workspace_fingerprint"', source)

    def test_engine_reads_workspace_identity_directly(self) -> None:
        import codey.completion.engine as engine_mod

        source = std_inspect.getsource(
            engine_mod.CompletionEngine._with_diagnostic_refs
        )
        self.assertIn("evidence.workspace_revision", source)
        self.assertIn("evidence.workspace_fingerprint", source)
        self.assertNotIn('getattr(evidence, "workspace_revision"', source)
        self.assertNotIn('getattr(evidence, "workspace_fingerprint"', source)

    def test_missing_identity_exposes_contract_error(self) -> None:
        from codey.completion.decision import build_completion_decision

        class _MissingIdentity:
            successful_checks = ()
            failed_checks_after_edit = ()
            environment_failures_after_edit = ()
            failed_checks = ()
            environment_failures = ()
            observed_tool_events = 0

        with tempfile.TemporaryDirectory() as td, self.assertRaises(AttributeError):
            build_completion_decision(
                run_id="run-1",
                stop_reason="done",
                task_changed=True,
                files=("app.py",),
                selected_check=None,
                evidence=_MissingIdentity(),  # type: ignore[arg-type]
                project=Path(td),
            )


class DuplicateProjectionMergeTests(unittest.TestCase):
    def test_topic_and_repair_share_one_codes_helper(self) -> None:
        import pathlib

        import codey.runs.trace as trace_mod

        source = pathlib.Path(trace_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("def _projection_codes", source)
        self.assertEqual(source.count("def _codes"), 0)
        self.assertGreaterEqual(source.count("_projection_codes("), 2)

    def test_codes_helper_bounds_unchanged(self) -> None:
        import codey.runs.trace as trace_mod

        source = std_inspect.getsource(trace_mod._projection_codes)
        self.assertIn("_safe_trace_code", source)
        self.assertIn("80", source)
        self.assertIn("MAX_WARNINGS", source)

    def test_decision_single_classify_call_with_env_failed(self) -> None:
        import codey.completion.decision as decision_mod

        source = std_inspect.getsource(decision_mod.build_completion_decision)
        self.assertEqual(source.count("classify_verification_failure("), 1)
        # environment failure must still force proof_status "failed"
        self.assertIn('"failed"', source)
        self.assertIn("environment is not None", source)


if __name__ == "__main__":
    unittest.main()
