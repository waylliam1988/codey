"""Full-red round4 sweep: overflow/bool/unicode/finite/schema/host/api/ledger guards.

Red-first: every bug lock in this file failed before the fix (OverflowError/
ValueError/500/wrong-accept) and passes after. One guard
(test_restore_unknown_path_is_409_conflict) passed before and after: it
locks the investigated non-bug that unknown restore rels map to 409
conflicts, never 500. Other non-bugs investigated (trailing-dot FQDN,
provenance over-approx, lenient `or` defaults, loopback auth, internal
pending shapes) are documented in the changelog with no lock.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from urllib.parse import urlparse


class OverflowSweepTests(unittest.TestCase):
    def test_concepts_bounded_int_inf(self):
        from codey.knowledge.concepts import _bounded_int
        self.assertEqual(_bounded_int(float("inf"), 12, 8, 200), 12)

    def test_research_interest_bounded_int_inf(self):
        from codey.knowledge.research_interest import _bounded_int
        self.assertEqual(_bounded_int(float("inf"), 12, 1, 32), 12)

    def test_research_interest_unit_float_huge(self):
        from codey.knowledge.research_interest import _unit_float
        self.assertEqual(_unit_float(10 ** 400), 0.0)

    def test_research_interest_hint_weight_huge(self):
        from codey.knowledge.research_interest import _hint_weight_by_target
        out = _hint_weight_by_target(
            [{"kind": "x", "target": "t", "weight": 10 ** 400, "confidence": 1.0}], kind="x")
        self.assertEqual(out.get("t"), 0.0)

    def test_ghost_numbers_huge(self):
        from codey.ghost.numbers import clamp_unit_float, coerce_unit_float
        self.assertIsNone(coerce_unit_float(10 ** 400))
        self.assertEqual(clamp_unit_float(10 ** 400), 0.0)

    def test_receipt_nonnegative_inf(self):
        from codey.runs.receipt import _nonnegative_int
        self.assertEqual(_nonnegative_int(float("inf")), 0)

    def test_ledger_projection_int_inf(self):
        from codey.runs.ledger_projection import _int, _optional_int
        self.assertEqual(_int(float("inf")), 0)
        self.assertIsNone(_optional_int(float("inf")))

    def test_ledger_int_or_none_inf(self):
        from codey.runs.ledger import _int_or_none
        self.assertIsNone(_int_or_none(float("inf")))

    def test_trace_int_or_none_inf(self):
        from codey.runs.trace_values import _int_or_none
        self.assertIsNone(_int_or_none(float("inf")))

    def test_trace_unit_float_huge(self):
        from codey.runs.trace_values import _unit_float
        self.assertEqual(_unit_float(10 ** 400), 0.0)

    def test_repair_context_nonnegative_inf(self):
        from codey.completion.repair_context import _nonnegative_int
        self.assertEqual(_nonnegative_int(float("inf")), 0)

    def test_analysis_run_inf(self):
        from codey.research.analysis_run import _bounded_duration, _optional_int
        self.assertIsNone(_bounded_duration(float("inf")))
        self.assertIsNone(_optional_int(float("inf")))

    def test_artifact_lineage_inf(self):
        from codey.research.artifact_lineage import _bounded_size
        self.assertEqual(_bounded_size(float("inf")), 0)

    def test_source_search_bounded_limit_inf(self):
        from codey.research.source_search import bounded_limit
        self.assertEqual(bounded_limit(float("inf")), 6)

    def test_tool_contract_coerce_float_huge(self):
        from codey.research.tool_contract import _coerce_float
        self.assertIsNone(_coerce_float(10 ** 400))

    def test_tools_as_int_float_huge(self):
        from codey.research.tools import _as_float, _as_int
        self.assertEqual(_as_int(float("inf"), 0), 0)
        self.assertIsNone(_as_float(10 ** 400))

    def test_query_planner_unit_float_huge(self):
        from codey.research.query_planner import _unit_float
        self.assertEqual(_unit_float(10 ** 400), 0.0)

    def test_followup_bounded_score_huge(self):
        from codey.research.followup_selection import bounded_score
        self.assertEqual(bounded_score(10 ** 400), 0.0)

    def test_source_document_compact_pages_inf(self):
        from codey.research.source_document import compact_pages
        self.assertEqual(compact_pages([float("inf")]), "")

    def test_connector_search_bounded_timeout_huge(self):
        from codey.research.connector_search import _bounded_timeout
        self.assertEqual(_bounded_timeout(10 ** 400), 4.0)

    def test_sleep_int_inf(self):
        from codey.ghost.sleep import _int
        self.assertEqual(_int(float("inf")), 0)

    def test_work_queue_future_ts_inf(self):
        from codey.ghost.work_queue import _future_ts
        out = _future_ts("2026-01-01T00:00:00Z", float("inf"))
        self.assertIsInstance(out, str)

    def test_proof_quality_inf(self):
        from codey.research.proof_quality import _bounded_score, _positive_ints
        self.assertEqual(_positive_ints([float("inf")]), ())
        self.assertEqual(_bounded_score(10 ** 400), 0.0)

    def test_source_gateway_as_int_inf(self):
        from codey.research.source_gateway import _as_int
        self.assertEqual(_as_int(float("inf"), 0), 0)

    def test_knowledge_graph_as_int_inf(self):
        from codey.knowledge.graph import _as_int
        self.assertEqual(_as_int(float("inf"), 0), 0)

    def test_headless_int_or_zero_inf(self):
        from codey.app.headless_runner import _int_or_zero
        self.assertEqual(_int_or_zero(float("inf")), 0)

    def test_knowledge_note_as_float_huge(self):
        from codey.knowledge.note import _as_float
        self.assertIsNone(_as_float(10 ** 400))

    def test_ui_state_int_huge(self):
        from codey.storage.ui_state_store import _int
        self.assertEqual(_int(10 ** 400), 0)

    def test_directive_hint_weight_huge(self):
        from codey.ghost.directive import _hint_weight_by_target
        out = _hint_weight_by_target(
            [{"kind": "x", "target": "t", "weight": 10 ** 400, "confidence": 1.0}], kind="x")
        self.assertEqual(out.get("t"), 0.0)

    def test_discovery_bare_float_does_not_crash(self):
        from codey.providers import discovery as dv
        cand = {"bottom_ratio": "abc", "area": "abc", "anchor_distance": "abc",
                "visible": True, "fingerprint": {"tag": "textarea", "role": "textbox",
                "contenteditable": False, "type": "", "classes": []}}
        fp = {"tag": "textarea", "role": "textbox", "contenteditable": False,
              "type": "", "classes": []}
        self.assertIsInstance(dv._score_message_box_candidate(cand, fp, "hello chat"), int)
        self.assertIsInstance(
            dv._score_send_button_candidate(cand, fp, "send", dv.SEND_BUTTON), int)


class BoolUnicodeFiniteTests(unittest.TestCase):
    def test_bool_is_not_one(self):
        from codey.completion.repair_context import _nonnegative_int as cr
        from codey.ghost.sleep import _int as sl
        from codey.research.source_search import bounded_limit as bl
        from codey.storage.conversation_store import _nonnegative_int as cs_ni
        from codey.storage.conversation_store import _positive_int as cs_pi
        self.assertEqual(cr(True), 0)
        self.assertEqual(cs_ni(True), 0)
        self.assertEqual(cs_pi(True, 7), 7)
        self.assertEqual(sl(True), 0)
        self.assertEqual(bl(True), 6)

    def test_ascii_digit_gate(self):
        from codey.research.proof_quality import _positive_ints as pi
        from codey.research.source_document import compact_pages as cp
        from codey.research.tool_contract import _coerce_int as tc
        from codey.research.tools import _as_int as rt
        from codey.runs.ledger_projection import _int as lp
        from codey.runs.receipt import _nonnegative_int as rc
        self.assertEqual(lp("١٢٣"), 0)
        self.assertEqual(rc("١٢٣"), 0)
        self.assertIsNone(tc("١٢٣"))
        self.assertEqual(rt("١٢٣", 0), 0)
        self.assertEqual(cp(["١٢٣"]), "")
        self.assertEqual(pi(["١٢٣"]), ())

    def test_coerce_float_finite(self):
        from codey.knowledge.note import _as_float as kn
        from codey.research.tool_contract import _coerce_float as tc
        from codey.research.tools import _as_float as rt
        self.assertIsNone(tc(float("inf")))
        self.assertIsNone(tc("nan"))
        self.assertIsNone(rt(float("inf")))
        self.assertIsNone(kn("inf"))

    def test_bounded_scores_finite(self):
        from codey.ghost.hebbian import _clamp01 as hb
        from codey.knowledge.research_interest import _unit_float as ri
        from codey.research.proof_quality import _bounded_score as pq
        from codey.runs.trace_values import _unit_float as tr
        self.assertEqual(pq(float("nan")), 0.0)
        self.assertEqual(pq("inf"), 0.0)
        self.assertEqual(ri(float("nan")), 0.0)
        self.assertEqual(tr(float("nan")), 0.0)
        self.assertEqual(hb(float("nan")), 0.0)

    def test_confidence_label_inf_is_unknown(self):
        from codey.ghost.control_surface import _confidence_label
        self.assertEqual(_confidence_label(float("inf")), "Unknown confidence")

    def test_local_config_rejects_underscore(self):
        from codey.providers.local_config import _parse_positive_int
        self.assertIsNone(_parse_positive_int("1_0"))
        self.assertIsNone(_parse_positive_int("1,000"))


class SchemaStrictTests(unittest.TestCase):
    def test_adapter_overrides_rejects_bool_float_version(self):
        from codey.repairs.adapter_overrides import _index_path, _load_index
        with tempfile.TemporaryDirectory() as td:
            p = _index_path("demo", td)
            p.parent.mkdir(parents=True, exist_ok=True)
            for bad in (True, 1.0):
                p.write_text(json.dumps({"schema_version": bad, "generations": {}}), encoding="utf-8")
                loaded = _load_index("demo", td).get("schema_version")
                self.assertIs(type(loaded), int)
                self.assertEqual(loaded, 1)

    def test_evidence_ledger_rejects_bool_float_version(self):
        from codey.research.evidence_ledger import EVIDENCE_LEDGER_KIND, _valid_ledger_payload
        base = {"kind": EVIDENCE_LEDGER_KIND, "records": [], "sources": {},
                "evidence": {}, "claims": {}, "assumptions": {}, "relations": {}}
        self.assertFalse(_valid_ledger_payload({**base, "schema_version": True}))
        self.assertFalse(_valid_ledger_payload({**base, "schema_version": 1.0}))
        self.assertTrue(_valid_ledger_payload({**base, "schema_version": 1}))

    def test_bounded_receipt_only_v1(self):
        from codey.app.headless_runner import _bounded_receipt
        good = {"schema_version": 1, "display": {"summary": "s", "detail": "d"}}
        self.assertEqual(_bounded_receipt(good).get("schema_version"), 1)
        for bad in (True, 0, 2, 1.0, "1", None):
            payload = {"schema_version": bad, "display": {"summary": "s", "detail": "d"}}
            self.assertNotIn("schema_version", _bounded_receipt(payload),
                             msg=f"bad version {bad!r} must not echo")


class HostUrlStrictTests(unittest.TestCase):
    def test_empty_saved_host_is_fail_closed(self):
        from codey.providers.controls import _host_matches
        self.assertFalse(_host_matches("evil.com", ""))
        self.assertFalse(_host_matches("", ""))

    def test_search_redirect_requires_dot_boundary(self):
        from codey.research.browser_search import _host_is_search_engine, _is_search_redirect, _search_redirect_target
        evil = urlparse("https://evilbing.com/ck/?u=https://evil.example/phish")
        self.assertFalse(_is_search_redirect(evil))
        self.assertEqual(_search_redirect_target(evil), "")
        self.assertFalse(_host_is_search_engine("evilbing.com"))
        evil2 = urlparse("https://notduckduckgo.com/?uddg=https://evil.example")
        self.assertFalse(_is_search_redirect(evil2))
        self.assertEqual(_search_redirect_target(evil2), "")

    def test_search_host_strips_port(self):
        from codey.research.browser_search import _search_host
        self.assertEqual(
            _search_host({"search_url": "https://www.bing.com:443/search?q={query}"}),
            "www.bing.com")


class ApiContractTests(unittest.TestCase):
    def test_changes_null_byte_is_400_not_500(self):
        from codey.app import api as api_mod

        class Ctx:
            def change_tracker_for(self, *a, **k):
                raise AssertionError("must not reach tracker")
        code, _ = api_mod.changes_response(Ctx(), "\0")
        self.assertEqual(code, 400)

    def test_restore_null_byte_is_400_not_500(self):
        from codey.app import api as api_mod

        class Ctx:
            def has_active_run_for_project(self, k):
                return False
            def acquire_project_writer(self, k):
                return True
            def release_project_writer(self, k):
                pass
            def change_tracker_for(self, *a, **k):
                raise AssertionError("must not reach tracker")
        code, _ = api_mod.restore_changes_response(Ctx(), {"project": "\0", "paths": []})
        self.assertEqual(code, 400)

    def test_restore_unknown_path_is_409_conflict(self):
        # Investigated non-bug (kept as guard): unknown rels are not in
        # _before so restore() maps them to conflicts -> 409, never 500.
        import tempfile
        from pathlib import Path

        from codey.app import api as api_mod
        from codey.workspace.changes import ChangeTracker

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("a\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            (root / "app.py").write_text("b\n", encoding="utf-8")

            class Ctx:
                def has_active_run_for_project(self, k):
                    return False
                def acquire_project_writer(self, k):
                    return True
                def release_project_writer(self, k):
                    pass
                def change_tracker_for(self, k, persistent=True):
                    return tracker
            code, payload = api_mod.restore_changes_response(
                Ctx(), {"project": td, "paths": ["../escape"]})
            self.assertEqual(code, 409)
            self.assertFalse(payload.get("ok", True))

    def test_base_revision_bool_and_list_are_400(self):
        from codey.app import api as api_mod

        class Ctx:
            def save_ui_state(self, state, base_revision=0):
                return {"revision": 1}
        for bad in (True, [], {}, "", None, 0.0):
            code, _ = api_mod.save_ui_state_response(
                Ctx(), {"state": {"sessions": []}, "base_revision": bad})
            self.assertEqual(code, 400, msg=f"base_revision={bad!r}")

    def test_shell_approval_non_bool_is_400(self):
        from codey.agents.shell_approval import shell_command_payload
        from codey.app import api as api_mod
        seen = []

        def make_pending(i):
            fields = shell_command_payload("echo hi")
            return {"run_id": "r", "session_id": "s", "id": i,
                    "command": "echo hi",
                    "command_preview": fields["command"],
                    "command_sha256": fields["command_sha256"],
                    "command_chars": fields["command_chars"],
                    "command_truncated": fields["command_truncated"],
                    "cwd": "/tmp"}

        class Ctx:
            def pop_pending_shell_approval(self, i):
                seen.append(i)
                return make_pending(i)
            def record_shell_result(self, e):
                pass
        for bad in (1, "true", "yes", 0):
            code, payload = api_mod.shell_approval_response(
                Ctx(), {"id": "x", "approved": bad},
                submit_task_after_slot_release=lambda *a, **k: None)
            self.assertEqual(code, 400, msg=f"approved={bad!r}")


class LedgerEventGuardTests(unittest.TestCase):
    def test_ledger_args_not_dict_does_not_crash(self):
        import tempfile
        from pathlib import Path

        from codey.runs.ledger import RunLedgerWriter
        from codey.runtime.core.models import ToolCall
        from codey.runtime.observe.events import RunEvent
        from codey.toolchain.runtime import ToolOutcome
        with tempfile.TemporaryDirectory() as td:
            for bad_args in (None, 123, "oops", [("path", "x")]):
                w = RunLedgerWriter(Path(td) / "ledger.jsonl",
                                    run_id="run-args-guard", session_id="sess")
                call = ToolCall(name="edit", args=bad_args)  # type: ignore[arg-type]
                outcome = ToolOutcome("ok", True, changed=True)
                w.append_run_event(RunEvent(kind="tool", turn=1, call=call, outcome=outcome))

    def test_events_render_args_not_dict_does_not_crash(self):
        from codey.runtime.core.models import ToolCall
        from codey.runtime.observe.events import RunEvent, render_run_event, run_event_ui_payload
        from codey.toolchain.runtime import ToolOutcome
        call = ToolCall(name="edit", args=None)  # type: ignore[arg-type]
        outcome = ToolOutcome("ok", True, changed=True)
        ev = RunEvent(kind="tool", turn=1, call=call, outcome=outcome)
        self.assertIsInstance(render_run_event(ev), str)
        payload = run_event_ui_payload("r", "s", ev)
        self.assertIsInstance(payload, dict)

    def test_session_log_mutate_bad_row_is_write_error(self):
        import tempfile

        from codey.runtime.log import entries as log_entries
        from codey.runtime.log.session_log import RuntimeSessionLog
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(td)
            with self.assertRaises(log_entries.RuntimeLogWriteError):
                log.mutate("s1", lambda proj, ents: [None])
            with self.assertRaises(log_entries.RuntimeLogWriteError):
                log.mutate("s1", lambda proj, ents: [123])


if __name__ == "__main__":
    unittest.main()
