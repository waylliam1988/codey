from __future__ import annotations

import ast
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codey.cli as cli
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore, conflict_key_for_signal, value_key_for_signal
from codey.ghost.schema import GhostSignal, GhostSignalParseResult
from codey.ghost.store import GhostSignalStore
from codey.server import State


ROOT = Path(__file__).resolve().parents[1]


def _signal(
    kind: str,
    *,
    scope: str = "user",
    summary: str = "Prefer concise answers.",
    quote: str = "以后回答短一点",
    confidence: float = 0.9,
    metadata: dict[str, object] | None = None,
) -> GhostSignal:
    return GhostSignal(
        kind=kind,
        scope=scope,
        summary=summary,
        evidence_quote=quote,
        confidence=confidence,
        metadata=metadata or {},
        source="test",
    )


def _result(*signals: GhostSignal) -> GhostSignalParseResult:
    return GhostSignalParseResult(signals=tuple(signals), ok=True, provider_id="test")


class GhostInboxStoreTests(unittest.TestCase):
    def test_signal_maps_to_candidate_and_high_confidence_style_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            created = store.ingest_signals(
                _result(_signal("style_preference")),
                session_id="s1",
                run_id="r1",
                project=td,
                user_text="以后回答短一点",
            )

            self.assertEqual(len(created), 1)
            candidate = created[0]
            self.assertEqual(candidate.candidate_type, "preference_candidate")
            self.assertEqual(candidate.signal_kind, "style_preference")
            self.assertEqual(candidate.status, "accepted")
            self.assertEqual(candidate.gate_reason, "high_confidence_style_preference")
            self.assertEqual(candidate.session_id, "s1")
            self.assertEqual(candidate.run_id, "r1")
            self.assertEqual(candidate.project, str(Path(td).resolve()))
            self.assertTrue(candidate.value_key)
            self.assertEqual(candidate.evidence_refs, (f"{candidate.id}:1",))

    def test_correction_is_candidate_even_when_confident(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            created = store.ingest_signals(
                _result(_signal(
                    "correction",
                    scope="session",
                    summary="The project should not port the torch module.",
                    quote="正确是这个项目不应该直接搬 torch 模块",
                    confidence=0.99,
                )),
                session_id="s1",
                run_id="r1",
                project=td,
                user_text="正确是这个项目不应该直接搬 torch 模块",
            )

            self.assertEqual(created[0].status, "candidate")
            self.assertEqual(created[0].gate_reason, "correction_requires_review")

    def test_interest_goal_and_action_tendency_remain_candidates(self) -> None:
        cases = (
            ("research_interest", "research_interest_candidate"),
            ("long_term_goal", "goal_candidate"),
            ("action_tendency", "action_tendency_candidate"),
        )
        for kind, candidate_type in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                store = GhostInboxStore(td)
                created = store.ingest_signals(
                    _result(_signal(kind, summary=f"{kind} summary", quote="以后跟进这个方向")),
                    session_id="s1",
                    run_id="r1",
                    project=td,
                    user_text="以后跟进这个方向",
                )

                self.assertEqual(created[0].candidate_type, candidate_type)
                self.assertEqual(created[0].status, "candidate")

    def test_low_confidence_safe_signal_is_stored_as_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            created = store.ingest_signals(
                _result(_signal("style_preference", confidence=0.2)),
                session_id="s1",
                run_id="r1",
                project=td,
                user_text="以后回答短一点",
            )

            self.assertEqual(created[0].status, "rejected")
            self.assertEqual(created[0].gate_reason, "confidence_below_candidate_threshold")
            self.assertEqual(store.list_candidates(status="rejected")[0].id, created[0].id)

    def test_sensitive_signal_writes_sanitized_rejection_event_but_not_inbox(self) -> None:
        secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            created = store.ingest_signals(
                _result(_signal(
                    "correction",
                    summary="Remember the user's API key.",
                    quote=f"API key 是 {secret}",
                    confidence=0.95,
                )),
                session_id="s1",
                run_id="r1",
                project=td,
            )

            self.assertEqual(created, ())
            self.assertEqual(store.list_candidates(), ())
            raw_events = store.events_path.read_text(encoding="utf-8")
            self.assertIn("ghost_memory_candidate_rejected", raw_events)
            self.assertNotIn(secret, raw_events)
            self.assertNotIn("Remember the user's API key", raw_events)

    def test_ungrounded_quote_is_rejected_without_projection_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            store.ingest_signals(
                _result(_signal("style_preference", quote="not in source text")),
                session_id="s1",
                run_id="r1",
                project=td,
                user_text="以后回答短一点",
            )

            self.assertEqual(store.list_candidates(), ())
            self.assertIn("evidence_quote_not_grounded", store.events_path.read_text(encoding="utf-8"))

    def test_duplicate_conflict_key_updates_existing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            signal = _signal(
                "style_preference",
                summary="Prefer answer-first replies.",
                quote="以后先给结论",
            )

            store.ingest_signals(_result(signal), session_id="s1", run_id="r1", project=td)
            store.ingest_signals(_result(signal), session_id="s1", run_id="r2", project=td)
            rows = store.list_candidates()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].reinforcement_count, 2)
            self.assertEqual(rows[0].run_id, "r2")
            self.assertEqual(rows[0].evidence_refs, (f"{rows[0].id}:1", f"{rows[0].id}:2"))

    def test_accepted_candidate_is_not_downgraded_by_lower_status_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            metadata = {"conflict_key": "reply_structure", "value_key": "answer_first"}
            store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Prefer answer first.",
                    quote="以后先给结论",
                    confidence=0.9,
                    metadata=metadata,
                )),
                session_id="s1",
                run_id="r1",
                project=td,
            )
            updated = store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Weak answer first signal.",
                    quote="以后先给结论",
                    confidence=0.2,
                    metadata=metadata,
                )),
                session_id="s1",
                run_id="r2",
                project=td,
            )

            self.assertEqual(updated[0].status, "accepted")
            self.assertEqual(store.list_candidates()[0].status, "accepted")

    def test_review_metadata_is_not_overwritten_by_ordinary_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            metadata = {"conflict_key": "reply_structure", "value_key": "answer_first"}
            created = store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Prefer answer first.",
                    quote="以后先给结论",
                    confidence=0.6,
                    metadata=metadata,
                )),
                session_id="s1",
                run_id="r1",
                project=td,
            )[0]
            reviewed = store.review_candidate(created.id, "accept", reviewed_by="test")
            assert reviewed is not None

            store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Prefer answer first.",
                    quote="以后先给结论",
                    confidence=0.95,
                    metadata=metadata,
                )),
                session_id="s1",
                run_id="r2",
                project=td,
            )
            row = store.list_candidates()[0]

            self.assertEqual(row.status, "accepted")
            self.assertEqual(row.gate_reason, "manual_accept")
            self.assertEqual(row.reviewed_by, "test")
            self.assertTrue(row.reviewed_at)

    def test_same_conflict_different_value_is_not_merged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Prefer answer first.",
                    quote="以后先给结论",
                    metadata={"conflict_key": "reply_structure", "value_key": "answer_first"},
                )),
                session_id="s1",
                run_id="r1",
                project=td,
            )
            store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Prefer detailed reasoning.",
                    quote="以后展开细节",
                    metadata={"conflict_key": "reply_structure", "value_key": "detailed"},
                )),
                session_id="s1",
                run_id="r2",
                project=td,
            )

            rows = store.list_candidates()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row.value_key for row in rows}, {"answer_first", "detailed"})
            self.assertEqual({row.status for row in rows}, {"accepted"})

    def test_review_candidate_accept_supersedes_conflicting_accepted_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            first = store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Prefer answer first.",
                    quote="以后先给结论",
                    metadata={"conflict_key": "reply_structure", "value_key": "answer_first"},
                )),
                session_id="s1",
                run_id="r1",
                project=td,
            )[0]
            second = store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Prefer detailed reasoning.",
                    quote="以后展开细节",
                    metadata={"conflict_key": "reply_structure", "value_key": "detailed"},
                )),
                session_id="s1",
                run_id="r2",
                project=td,
            )[0]

            reviewed = store.review_candidate(second.id, "accept", reviewed_by="test")
            rows = {row.id: row for row in store.list_candidates()}

            self.assertIsNotNone(reviewed)
            self.assertEqual(rows[second.id].status, "accepted")
            self.assertEqual(rows[second.id].reviewed_by, "test")
            self.assertEqual(rows[first.id].status, "superseded")
            self.assertEqual(rows[first.id].superseded_by, second.id)
            events = store.events_path.read_text(encoding="utf-8")
            self.assertIn("ghost_memory_candidate_reviewed", events)
            self.assertIn("superseded", events)

    def test_superseded_candidate_is_not_revived_by_ordinary_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            first_signal = _signal(
                "style_preference",
                summary="Prefer answer first.",
                quote="以后先给结论",
                metadata={"conflict_key": "reply_structure", "value_key": "answer_first"},
            )
            first = store.ingest_signals(
                _result(first_signal),
                session_id="s1",
                run_id="r1",
                project=td,
            )[0]
            second = store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Prefer detailed reasoning.",
                    quote="以后展开细节",
                    metadata={"conflict_key": "reply_structure", "value_key": "detailed"},
                )),
                session_id="s1",
                run_id="r2",
                project=td,
            )[0]
            store.review_candidate(second.id, "accept", reviewed_by="test")

            store.ingest_signals(
                _result(first_signal),
                session_id="s1",
                run_id="r3",
                project=td,
            )
            rows = {row.id: row for row in store.list_candidates()}

            self.assertEqual(rows[first.id].status, "superseded")
            self.assertEqual(rows[first.id].superseded_by, second.id)

    def test_events_compact_when_byte_limit_is_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            signal = _signal(
                "style_preference",
                summary="Prefer answer-first replies. " + ("x" * 500),
                quote="以后先给结论",
            )
            store.ingest_signals(_result(signal), session_id="s1", run_id="r1", project=td)
            first_size = store.events_path.stat().st_size

            with mock.patch("codey.ghost.inbox.MAX_EVENTS_BYTES", first_size + 500):
                store.ingest_signals(_result(signal), session_id="s1", run_id="r2", project=td)

            self.assertLessEqual(store.events_path.stat().st_size, first_size + 500)
            self.assertLessEqual(len(store.events_path.read_text(encoding="utf-8").splitlines()), 2)
            self.assertEqual(store.list_candidates()[0].reinforcement_count, 2)

    def test_oversize_events_warning_does_not_write_empty_projection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            store.ingest_signals(
                _result(_signal("style_preference")),
                session_id="s1",
                run_id="r1",
                project=td,
            )
            store.inbox_path.unlink()

            with mock.patch("codey.ghost.inbox.MAX_EVENTS_BYTES", 1):
                rows = store.list_candidates()

            self.assertEqual(rows, ())
            self.assertEqual(store.last_warnings, ("events_too_large",))
            self.assertFalse(store.inbox_path.exists())

    def test_ingest_does_not_overwrite_projection_when_events_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            store.ingest_signals(
                _result(_signal(
                    "style_preference",
                    summary="Old candidate",
                    quote="以后回答短一点",
                )),
                session_id="s1",
                run_id="r1",
                project=td,
            )
            old_events = store.events_path.read_text(encoding="utf-8")
            store.inbox_path.unlink()

            with mock.patch("codey.ghost.inbox.MAX_EVENTS_BYTES", 1):
                created = store.ingest_signals(
                    _result(_signal(
                        "style_preference",
                        summary="New candidate",
                        quote="以后先给结论",
                    )),
                    session_id="s1",
                    run_id="r2",
                    project=td,
                )

            self.assertEqual(created, ())
            self.assertEqual(store.last_warnings, ("events_too_large",))
            self.assertFalse(store.inbox_path.exists())
            self.assertEqual(store.events_path.read_text(encoding="utf-8"), old_events)

    def test_conflict_key_can_use_structured_metadata_without_local_language_rules(self) -> None:
        signal = _signal(
            "style_preference",
            metadata={"conflict_key": "reply_structure", "value_key": "answer_first"},
        )

        self.assertEqual(conflict_key_for_signal(signal), "style_preference:reply_structure")
        self.assertEqual(value_key_for_signal(signal), "answer_first")

    def test_applicable_candidates_order_session_project_user(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            store.ingest_signals(
                _result(_signal("style_preference", summary="User preference", quote="以后回答短一点")),
                session_id="s1",
                run_id="r-user",
                project=td,
            )
            store.ingest_signals(
                _result(_signal(
                    "research_interest",
                    scope="project",
                    summary="Project interest",
                    quote="这个项目要继续研究 inbox",
                )),
                session_id="s1",
                run_id="r-project",
                project=td,
            )
            store.ingest_signals(
                _result(_signal(
                    "correction",
                    scope="session",
                    summary="Session correction",
                    quote="这轮正确是先做 inbox",
                )),
                session_id="s1",
                run_id="r-session",
                project=td,
            )

            rows = store.applicable_candidates(project=td, session_id="s1")

            self.assertEqual([row.scope for row in rows], ["session", "project", "user"])

    def test_applicable_candidates_keep_newest_first_within_same_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            with mock.patch("codey.ghost.inbox._now", return_value="2026-01-01T00:00:01Z"):
                store.ingest_signals(
                    _result(_signal(
                        "style_preference",
                        summary="Old user preference",
                        quote="以后回答短一点",
                    )),
                    session_id="s1",
                    run_id="r-old",
                    project=td,
                )
            with mock.patch("codey.ghost.inbox._now", return_value="2026-01-01T00:00:02Z"):
                store.ingest_signals(
                    _result(_signal(
                        "style_preference",
                        summary="New user preference",
                        quote="以后回答先给结论",
                    )),
                    session_id="s1",
                    run_id="r-new",
                    project=td,
                )

            rows = store.applicable_candidates(project=td, session_id="s1")

            self.assertEqual([row.summary for row in rows], ["New user preference", "Old user preference"])

    def test_project_scope_does_not_leak_to_other_project(self) -> None:
        with tempfile.TemporaryDirectory() as state_td, tempfile.TemporaryDirectory() as p1, tempfile.TemporaryDirectory() as p2:
            store = GhostInboxStore(state_td)
            store.ingest_signals(
                _result(_signal(
                    "research_interest",
                    scope="project",
                    summary="Project-only memory",
                    quote="这个项目记住 inbox 方向",
                )),
                session_id="s1",
                run_id="r1",
                project=p1,
            )

            self.assertEqual(store.applicable_candidates(project=p2, session_id="s1"), ())
            self.assertEqual(len(store.list_candidates(scope="project", project=p1)), 1)
            self.assertEqual(store.list_candidates(scope="project", project=p2), ())

    def test_delete_scope_only_removes_target_and_compacts_event_text(self) -> None:
        with tempfile.TemporaryDirectory() as state_td, tempfile.TemporaryDirectory() as project_td:
            store = GhostInboxStore(state_td)
            store.ingest_signals(
                _result(_signal("style_preference", summary="User memory", quote="以后回答短一点")),
                session_id="s1",
                run_id="r-user",
                project=project_td,
            )
            store.ingest_signals(
                _result(_signal(
                    "research_interest",
                    scope="project",
                    summary="Unique project deletion phrase",
                    quote="这个项目要记住 unique deletion quote",
                )),
                session_id="s1",
                run_id="r-project",
                project=project_td,
            )

            removed = store.delete_scope("project", project=project_td)

            self.assertEqual(removed, 1)
            self.assertEqual([row.scope for row in store.list_candidates()], ["user"])
            self.assertNotIn("Unique project deletion phrase", store.inbox_path.read_text(encoding="utf-8"))
            self.assertNotIn("unique deletion quote", store.events_path.read_text(encoding="utf-8"))

    def test_export_and_reset_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            store.ingest_signals(
                _result(_signal("style_preference")),
                session_id="s1",
                run_id="r1",
                project=td,
            )

            exported = store.export_state()
            self.assertEqual(len(exported["inbox"]["candidates"]), 1)
            self.assertTrue(store.reset_all())
            self.assertEqual(store.list_candidates(), ())
            self.assertFalse(store.events_path.exists())

    def test_bad_projection_is_quarantined_and_rebuilt_from_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            store.ingest_signals(
                _result(_signal("style_preference")),
                session_id="s1",
                run_id="r1",
                project=td,
            )
            store.inbox_path.write_text("{bad json", encoding="utf-8")

            rows = store.list_candidates()

            self.assertEqual(len(rows), 1)
            self.assertTrue(list(store.directory.glob("inbox.json.quarantine.*")))

    def test_future_projection_schema_is_quarantined_not_silently_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            store.directory.mkdir(parents=True, exist_ok=True)
            store.inbox_path.write_text(
                json.dumps({
                    "schema_version": 999,
                    "kind": "ghost_memory_inbox_projection",
                    "candidates": [{
                        "id": "future",
                        "candidate_type": "preference_candidate",
                        "signal_kind": "style_preference",
                        "status": "accepted",
                        "scope": "user",
                        "summary": "future",
                        "evidence_quote": "future",
                        "confidence": 1.0,
                        "conflict_key": "style_preference:future",
                    }],
                }),
                encoding="utf-8",
            )

            self.assertEqual(store.list_candidates(), ())
            self.assertTrue(list(store.directory.glob("inbox.json.quarantine.*")))

    def test_bad_event_lines_are_skipped_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            store.ingest_signals(
                _result(_signal("style_preference")),
                session_id="s1",
                run_id="r1",
                project=td,
            )
            with store.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("{bad json\n")
            store.inbox_path.unlink()

            rows = store.list_candidates()

            self.assertEqual(len(rows), 1)
            self.assertTrue(any("bad_json" in item for item in store.last_warnings))

    def test_projection_write_failure_is_fail_open_and_rebuildable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)
            with mock.patch("codey.ghost.inbox.write_json_atomic", side_effect=OSError("disk full")):
                created = store.ingest_signals(
                    _result(_signal("style_preference")),
                    session_id="s1",
                    run_id="r1",
                    project=td,
                )

            self.assertEqual(len(created), 1)
            self.assertEqual(len(store.list_candidates()), 1)

    def test_learning_setting_reports_audit_event_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)

            with mock.patch.object(store, "_append_events", return_value=False):
                ok = store.set_learning_enabled(False)

            self.assertFalse(ok)
            self.assertFalse(store.learning_enabled())

    def test_disable_blocks_future_ingest_but_list_and_export_still_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostInboxStore(td)

            self.assertTrue(store.set_learning_enabled(False))
            self.assertFalse(store.learning_enabled())
            self.assertEqual(
                store.ingest_signals(
                    _result(_signal("style_preference")),
                    session_id="s1",
                    run_id="r1",
                    project=td,
                ),
                (),
            )
            self.assertEqual(store.list_candidates(), ())
            self.assertEqual(store.export_state()["settings"]["learning_enabled"], False)

            self.assertTrue(store.set_learning_enabled(True))
            self.assertEqual(
                len(store.ingest_signals(
                    _result(_signal("style_preference")),
                    session_id="s1",
                    run_id="r2",
                    project=td,
                )),
                1,
            )

    def test_bare_state_disables_ghost_inbox(self) -> None:
        self.assertIsNone(State().ghost_inbox)
        self.assertIsNone(State().ghost_hebbian)


class GhostSignalStoreScopeTests(unittest.TestCase):
    def test_signal_store_delete_scope_filters_raw_signal_audit(self) -> None:
        with tempfile.TemporaryDirectory() as state_td, tempfile.TemporaryDirectory() as project_td:
            store = GhostSignalStore(state_td)
            store.append_extraction(
                _result(
                    _signal("style_preference", summary="User signal", quote="以后回答短一点"),
                    _signal(
                        "research_interest",
                        scope="project",
                        summary="Project signal should disappear",
                        quote="这个项目要记住 raw audit",
                    ),
                ),
                session_id="s1",
                run_id="r1",
                project=project_td,
            )

            removed = store.delete_scope("project", project=project_td)

            self.assertEqual(removed, 1)
            rows = store.read_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual([signal["scope"] for signal in rows[0]["signals"]], ["user"])
            self.assertNotIn("Project signal should disappear", store.path.read_text(encoding="utf-8"))

    def test_signal_store_delete_scope_removes_empty_raw_audit_rows(self) -> None:
        with tempfile.TemporaryDirectory() as state_td:
            store = GhostSignalStore(state_td)
            store.append_extraction(
                _result(_signal("style_preference", summary="User signal", quote="以后回答短一点")),
                session_id="s1",
                run_id="r1",
                project=state_td,
            )

            removed = store.delete_scope("user")

            self.assertEqual(removed, 1)
            self.assertEqual(store.read_all(), ())
            self.assertEqual(store.path.read_text(encoding="utf-8"), "")


class GhostCliTests(unittest.TestCase):
    def test_ghost_help_mentions_signals_for_export_and_reset(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["ghost", "--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("export Ghost inbox/events/signals/state", help_text)
        self.assertIn("delete Ghost inbox/events/signals/state", help_text)
        self.assertIn("accept", help_text)
        self.assertIn("rebuild-state", help_text)

    def test_ghost_export_and_reset_cover_raw_signal_audit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            signal_store = GhostSignalStore(td)
            signal_store.append_extraction(
                _result(_signal("style_preference")),
                session_id="s1",
                run_id="r1",
                project=td,
            )
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                export_code = cli.main(["ghost", "export", "--state-home", td])
            export_payload = json.loads(stdout.getvalue())

            self.assertEqual(export_code, 0)
            self.assertEqual(len(export_payload["signals"]), 1)
            self.assertIn("hebbian", export_payload)

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                reset_code = cli.main(["ghost", "reset", "--state-home", td, "--yes"])
            reset_payload = json.loads(stdout.getvalue())

            self.assertEqual(reset_code, 0)
            self.assertTrue(reset_payload["ok"])
            self.assertFalse(signal_store.path.exists())

    def test_ghost_accept_reject_state_and_rebuild_state_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            candidate = inbox.ingest_signals(
                _result(_signal(
                    "correction",
                    summary="Use the local provider.",
                    quote="正确是使用 local provider",
                    confidence=0.95,
                    metadata={"conflict_key": "provider", "value_key": "local"},
                )),
                session_id="s1",
                run_id="r1",
                project=td,
            )[0]

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                accept_code = cli.main(["ghost", "accept", "--state-home", td, candidate.id])
            accept_payload = json.loads(stdout.getvalue())

            self.assertEqual(accept_code, 0)
            self.assertTrue(accept_payload["ok"])
            self.assertTrue(accept_payload["hebbian"]["applied"])
            self.assertEqual(len(GhostHebbianStore(td).list_nodes()), 1)

            GhostHebbianStore(td).state_path.unlink()
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                rebuild_code = cli.main(["ghost", "rebuild-state", "--state-home", td, "--yes"])
            rebuild_payload = json.loads(stdout.getvalue())

            self.assertEqual(rebuild_code, 0)
            self.assertTrue(rebuild_payload["ok"])

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                state_code = cli.main(["ghost", "state", "--state-home", td])
            state_payload = json.loads(stdout.getvalue())

            self.assertEqual(state_code, 0)
            self.assertTrue(state_payload["ok"])
            self.assertEqual(len(state_payload["state"]["nodes"]), 1)

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                reject_code = cli.main(["ghost", "reject", "--state-home", td, candidate.id])
            reject_payload = json.loads(stdout.getvalue())

            self.assertEqual(reject_code, 0)
            self.assertTrue(reject_payload["ok"])
            self.assertEqual(reject_payload["candidate"]["status"], "rejected")
            self.assertEqual(reject_payload["hebbian_removed"], {"edges": 0, "nodes": 1})
            self.assertEqual(GhostHebbianStore(td).list_nodes(status="active"), ())

    def test_ghost_accept_cli_creates_coactivation_edges_for_same_run_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            first = inbox.ingest_signals(
                _result(_signal(
                    "correction",
                    summary="Use the local provider.",
                    quote="正确是使用 local provider",
                    confidence=0.95,
                    metadata={"conflict_key": "provider", "value_key": "local"},
                )),
                session_id="s1",
                run_id="r1",
                project=td,
            )[0]
            second = inbox.ingest_signals(
                _result(_signal(
                    "correction",
                    summary="Keep the response concise.",
                    quote="正确是回答要简洁",
                    confidence=0.95,
                    metadata={"conflict_key": "reply_length", "value_key": "concise"},
                )),
                session_id="s1",
                run_id="r1",
                project=td,
            )[0]

            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(cli.main(["ghost", "accept", "--state-home", td, first.id]), 0)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                self.assertEqual(cli.main(["ghost", "accept", "--state-home", td, second.id]), 0)
            payload = json.loads(stdout.getvalue())

            self.assertTrue(payload["hebbian"]["applied"])
            self.assertEqual(len(payload["hebbian"]["edges"]), 1)
            self.assertEqual(len(GhostHebbianStore(td).list_edges()), 1)

    def test_ghost_delete_scope_reports_storage_errors_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            stdout = io.StringIO()
            with (
                mock.patch("sys.stdout", stdout),
                mock.patch(
                    "codey.ghost.inbox.GhostInboxStore.delete_scope",
                    side_effect=OSError("disk unavailable"),
                ),
            ):
                code = cli.main(["ghost", "delete-scope", "--state-home", td, "user", "--yes"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertIn("disk unavailable", payload["error"])

    def test_ghost_list_cli_outputs_json_without_provider_stack(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = (
                "import json, sys\n"
                "import codey.cli as cli\n"
                f"code = cli.main(['ghost','list','--state-home',r'{td}'])\n"
                "print(json.dumps({"
                "'code': code, "
                "'browser': 'codey.browser' in sys.modules, "
                "'providers': 'codey.providers.registry' in sys.modules, "
                "'tool_runtime': 'codey.tool_runtime' in sys.modules"
                "}))\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(ROOT),
                text=True,
                capture_output=True,
                check=True,
            )

        rows = completed.stdout.splitlines()
        payload = json.loads(rows[0])
        loaded = json.loads(rows[-1])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(loaded, {
            "code": 0,
            "browser": False,
            "providers": False,
            "tool_runtime": False,
        })


class GhostInboxArchitectureTests(unittest.TestCase):
    def test_ghost_modules_do_not_import_tool_runtime_or_provider_stack(self) -> None:
        forbidden = {
            "codey.tool_runtime",
            "codey.browser",
            "codey.providers",
            "torch",
            "transformers",
        }
        for path in (ROOT / "codey" / "ghost").glob("*.py"):
            with self.subTest(path=path.name):
                imports = _imported_modules(path)
                self.assertFalse(_matches_any_prefix(imports, forbidden))

    def test_tool_runtime_and_research_do_not_import_ghost(self) -> None:
        paths = [ROOT / "codey" / "tool_runtime.py", *(ROOT / "codey" / "research").glob("*.py")]
        for path in paths:
            with self.subTest(path=path.name):
                imports = _imported_modules(path)
                self.assertNotIn("codey.ghost", imports)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _matches_any_prefix(modules: set[str], prefixes: set[str]) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".")
        for module in modules
        for prefix in prefixes
    )


if __name__ == "__main__":
    unittest.main()
