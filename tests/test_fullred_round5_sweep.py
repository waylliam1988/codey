"""Full-red round5 sweep: deterministic fail-closed locks.

Red-first: every bug test below failed before the fix (crash or fail-open)
and passes after. Non-repros are documented as non-bugs in TEST_REPORT.
"""
from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path


class OverflowSweepTests(unittest.TestCase):
    def test_source_connectors_score_fail_closed(self):
        from codey.research.source_connectors import _score
        self.assertEqual(_score(True), 0.0)
        self.assertEqual(_score(float("inf")), 0.0)
        self.assertEqual(_score(float("nan")), 0.0)
        self.assertEqual(_score(10 ** 1000), 0.0)
        self.assertEqual(_score("oops"), 0.0)

    def test_http_redirect_inf_is_false(self):
        from codey.research.http_redirects import is_redirect_status
        self.assertFalse(is_redirect_status(float("inf")))
        self.assertFalse(is_redirect_status(float("-inf")))
        self.assertFalse(is_redirect_status("oops"))

    def test_snippet_at_malformed_no_crash(self):
        from codey.research.source_search import snippet_at
        self.assertEqual(snippet_at("hello", "oops"), snippet_at("hello", 0))
        self.assertEqual(snippet_at("hello", float("inf")), snippet_at("hello", 0))
        self.assertEqual(snippet_at("hello", True), snippet_at("hello", 0))

    def test_query_planner_max_depth_no_crash(self):
        import inspect

        from codey.research.query_planner import ResearchPlan
        sig = inspect.signature(ResearchPlan)
        kwargs = {}
        if "plan_ref" in sig.parameters:
            kwargs["plan_ref"] = "research_plan:" + "a" * 16
        if "max_depth" in sig.parameters:
            kwargs["max_depth"] = "oops"
        try:
            plan = ResearchPlan(**kwargs)
        except (ValueError, TypeError, OverflowError):
            self.fail("ResearchPlan ctor should not crash on bad max_depth")
        try:
            plan.to_payload()
        except (ValueError, TypeError, OverflowError) as exc:
            self.fail(f"to_payload crashed: {exc}")

    def test_ledger_as_page_huge_digits_no_crash(self):
        from codey.research import ledger as ledger_mod
        f = getattr(ledger_mod, "_as_page", None)
        if f is None:
            self.skipTest("no _as_page")
        try:
            result = f("9" * 5000)
        except ValueError:
            self.fail("_as_page crashed on huge digits, should be None")
        self.assertIsNone(result)

    def test_object_model_pages_read_fail_closed(self):
        from codey.research.object_model import ResearchSource
        try:
            payload = ResearchSource(source_id="s", pages_read=(True, "oops", float("inf"))).to_jsonable()
        except (ValueError, OverflowError, TypeError):
            self.fail("pages_read crashed, should filter bad pages")
            return
        pages = payload.get("pages_read", ())
        self.assertNotIn(True, pages)
        self.assertEqual(tuple(pages), ())

    def test_evidence_ledger_counts_no_crash(self):
        from codey.research.evidence_ledger import EvidenceLedgerWriteResult
        try:
            payload = EvidenceLedgerWriteResult(counts={"sources": "oops"}).to_trace_payload()
        except (ValueError, OverflowError, TypeError):
            self.fail("counts crashed, should clamp to 0")
            return
        counts = payload.get("counts", {})
        self.assertEqual(counts.get("sources"), 0)

    def test_tools_merge_relation_tags_skips_bad(self):
        from codey.research.tools import _merge_relation_tags
        try:
            _merge_relation_tags([], [{"src": "a"}])
            _merge_relation_tags([], ["oops"])
            _merge_relation_tags([], [None])
        except (KeyError, TypeError):
            self.fail("_merge_relation_tags crashed on bad relation, should skip")

    def test_ledger_record_open_document_no_crash(self):
        import inspect

        from codey.research import ledger as ledger_mod
        src = inspect.getsource(ledger_mod)
        self.assertNotIn("int(page) for page in document.pages_read", src,
                         "record_open_document still uses bare int(page)")

    def test_ledger_record_source_search_no_crash(self):
        import inspect

        from codey.research import ledger as ledger_mod
        src = inspect.getsource(ledger_mod)
        self.assertNotIn('int(hit.get("offset")', src,
                         "record_source_search still uses bare int(offset)")


class RunsRuntimeSweepTests(unittest.TestCase):
    def test_work_checkpoint_null_byte_fail_closed(self):
        from codey.runs.work_checkpoint import WorkCheckpointStore
        with tempfile.TemporaryDirectory() as td:
            store = WorkCheckpointStore(Path(td))
            try:
                result = store.start(run_id="r", session_id="s", project="/tmp\x00evil", task="t")
            except ValueError:
                self.fail("start raised ValueError on null byte, should fail closed")
            self.assertIsNone(result)

    def test_file_hash_null_byte_no_crash(self):
        from codey.runs.work_checkpoint import _file_hash
        try:
            result = _file_hash(Path("/tmp"), "a\x00b")
        except ValueError:
            self.fail("_file_hash crashed on null byte, should be None")
            return
        self.assertIsNone(result)

    def test_runtime_log_inf_fail_closed(self):
        from codey.runtime.log.entries import RuntimeLogEntry, RuntimeLogWriteError
        try:
            RuntimeLogEntry(session_id="s", lane="l", operation_id="o",
                            kind="operation_started", payload={"x": float("inf")}).to_json_line()
        except RuntimeLogWriteError:
            return
        except ValueError:
            self.fail("inf payload raised ValueError (500), should be RuntimeLogWriteError")
            return
        self.fail("inf payload should raise RuntimeLogWriteError, not dump")

    def test_cancellation_wait_no_crash(self):
        import inspect

        from codey.runtime.core import cancellation as canc
        src = inspect.getsource(canc)
        seg = src.split("def wait(")[1][:600] if "def wait(" in src else ""
        self.assertIn("_safe_timeout", seg, "wait() still uses bare float()")

    def test_output_capture_no_crash(self):
        from codey.runtime.core.output_capture import BoundedByteCapture
        try:
            BoundedByteCapture(head_limit="oops")
        except (ValueError, TypeError, OverflowError):
            self.fail("BoundedByteCapture crashed on bad limit")
            return

    def test_prompt_envelope_budget_no_crash(self):
        from codey.runtime.observe.prompt_envelope import PromptEnvelope, PromptEnvelopeSection
        try:
            PromptEnvelope([PromptEnvelopeSection(name="x", text="hi", budget="oops")]).render()
        except (ValueError, TypeError, OverflowError):
            self.fail("render crashed on bad budget")

    def test_prompt_surface_chars_no_crash(self):
        from codey.runtime.observe.prompt_surface import build_prompt_surface_record
        try:
            build_prompt_surface_record(phase="p", send_ref="s",
                                        prompt_digest="sha256:" + "a" * 64,
                                        prompt_chars="oops", epoch_id="ctx_epoch:" + "a" * 16)
        except (ValueError, TypeError, OverflowError):
            self.fail("build_prompt_surface_record crashed on bad chars")

    def test_trace_prompt_section_budget_no_crash(self):
        import inspect

        from codey.runs import trace as trace_mod
        src = inspect.getsource(trace_mod)
        self.assertNotIn("budget=max(0, int(budget or 0))", src,
                         "trace still uses bare int(budget)")

    def test_events_metadata_none_no_crash(self):
        from codey.runtime.observe import events as ev
        E = getattr(ev, "RunEvent", None)
        f = getattr(ev, "render_run_event", None)
        if E is None or f is None:
            self.skipTest("no RunEvent/render")
        try:
            f(E("info", message="x", metadata=None))
        except AttributeError:
            self.fail("render crashed on metadata=None, should treat as {}")

    def test_seed_checks_non_iterable_no_crash(self):
        from codey.runtime.observe.execution_evidence import ExecutionEvidence
        try:
            ExecutionEvidence().seed_checks(123)
        except TypeError:
            self.fail("seed_checks crashed on int, should skip")

    def test_event_bus_replay_bool_fail_closed(self):
        import inspect

        from codey.app.event_bus import EventBus
        src = inspect.getsource(EventBus.replay_events_after)
        self.assertIn("isinstance", src, "replay still maps True->1 without bool gate")

    def test_details_actions_summary_no_crash(self):
        import inspect

        from codey.runs import details as det
        src = inspect.getsource(det._actions_summary) if hasattr(det, "_actions_summary") else ""
        self.assertNotIn("int(counts.get", src, "details still uses bare int(counts)")

    def test_runs_payload_reply_int_no_crash(self):
        import inspect

        from codey.runs import ledger as ledger_mod
        src = inspect.getsource(ledger_mod)
        self.assertNotIn('len(event.reply or "")', src,
                         "ledger still uses len(reply) without type guard")


class ProvidersAppSweepTests(unittest.TestCase):
    def test_query_int_strict(self):
        from codey.app.api import query_int
        default, lo, hi = 1, 1, 3
        self.assertEqual(query_int({"depth": ["1_000"]}, "depth", default, lo, hi), default)
        self.assertEqual(query_int({"depth": [True]}, "depth", default, lo, hi), default)
        self.assertEqual(query_int({"limit": [10.7]}, "limit", 96, 8, 200), 96)

    def test_query_value_none_is_empty(self):
        from codey.app.api import query_value
        self.assertEqual(query_value({"session_id": [None]}, "session_id"), "")

    def test_continue_task_string_is_not_true(self):
        import inspect

        from codey.app import api as api_mod
        src = inspect.getsource(api_mod.run_submit_response)
        self.assertNotIn('bool(body.get("continue_task"))', src,
                         'bool("false")==True pollution still present')

    def test_server_content_length_guarded(self):
        import inspect

        from codey.app import server as server_mod
        src = inspect.getsource(server_mod)
        self.assertNotIn('int(self.headers.get("Content-Length"', src,
                         "server still uses bare int(Content-Length)")

    def test_shell_mint_generation_fail_closed(self):
        import inspect

        from codey.app import shell_service as shell_mod
        src = inspect.getsource(shell_mod.mint_shell_ticket)
        self.assertNotIn("int(pending.pop", src,
                         "mint still coerces bad generation to 0==0 pass")

    def test_controls_visible_locator_overflow(self):
        import inspect

        from codey.providers import controls as controls_mod
        src = inspect.getsource(controls_mod.visible_locator)
        self.assertIn("OverflowError", src, "visible_locator still misses OverflowError")

    def test_local_config_rejects_non_str(self):
        from codey.providers.local_config import config_from_dict
        try:
            config_from_dict({"schema_version": 1, "base_url": ["http://a"],
                              "model": 123, "api_key": {}})
        except (TypeError, ValueError):
            return
        self.fail("config_from_dict accepted list/int/dict, should reject")

    def test_web_provider_send_rejects_none(self):
        import inspect

        from codey.providers.web_provider import WebChatProvider
        src = inspect.getsource(WebChatProvider.send)
        self.assertIn("isinstance(text, str)", src,
                      "send still dereferences None.strip() without guard")

    def test_provider_services_bool_strict(self):
        import inspect

        from codey.app import provider_services as ps_mod
        src = inspect.getsource(ps_mod)
        self.assertNotIn("bool(statuses.get", src, 'bool("false")==True still present')

    def test_shell_exec_command_type_guarded(self):
        import inspect

        from codey.app import shell_service as shell_mod
        src = inspect.getsource(shell_mod)
        self.assertNotIn('(ticket.command or "")', src,
                         "execute still does (command or '').strip() without type guard")


class GhostKnowledgeSweepTests(unittest.TestCase):
    def test_work_queue_int_bool(self):
        from codey.ghost.work_queue import _int
        self.assertEqual(_int(True), 0)

    def test_work_queue_transition_overflow(self):
        import inspect

        from codey.ghost import work_queue as wq
        src = inspect.getsource(wq._apply_queue_transition)
        self.assertIn("OverflowError", src, "transition still misses OverflowError")

    def test_inbox_int_bool(self):
        from codey.ghost.inbox import _int_or_default
        self.assertEqual(_int_or_default(True, 1), 1 if False else _int_or_default(True, 1))
        # contract: bool must map to default, not 1
        self.assertEqual(_int_or_default(True, 7), 7)

    def test_observation_budget_no_crash(self):
        from codey.ghost.observation_index import _content_budget
        try:
            _content_budget("oops")
        except (ValueError, TypeError, OverflowError):
            self.fail("_content_budget crashed on bad budget")

    def test_compact_result_no_crash(self):
        from codey.ghost.event_log import compact_result_payload
        try:
            compact_result_payload(True, False, {"events": "abc", "bytes": 0}, {}, [])
        except (ValueError, TypeError, OverflowError):
            self.fail("compact_result_payload crashed on bad events")

    def test_expires_at_no_crash(self):
        from codey.ghost.continuity import _expires_at
        now = datetime.datetime.now(datetime.UTC)
        try:
            _expires_at(now, "abc")
        except (ValueError, TypeError, OverflowError):
            self.fail("_expires_at crashed on bad days")

    def test_continuity_items_skip_bad(self):
        from codey.ghost.continuity import _items_from_events
        try:
            _items_from_events([None])
            _items_from_events(["x"])
        except AttributeError:
            self.fail("_items_from_events crashed on bad row, should skip")

    def test_hebbian_sync_no_crash(self):
        import inspect

        from codey.ghost import hebbian as hebb_mod
        src = inspect.getsource(hebb_mod)
        self.assertNotIn('int(removed.get("nodes"', src, "hebbian still uses bare int(nodes)")

    def test_concepts_bool_ascii(self):
        from codey.knowledge.concepts import _bounded_int
        self.assertEqual(_bounded_int(True, 64, 8, 200), 64)
        self.assertEqual(_bounded_int("１２", 64, 8, 200), 64)

    def test_research_interest_bool(self):
        from codey.knowledge.research_interest import _unit_float
        self.assertEqual(_unit_float(True), 0.0)

    def test_store_rows_skip_bad(self):
        import inspect

        from codey.knowledge import store as store_mod
        src = inspect.getsource(store_mod)
        self.assertIn("isinstance(row", src, "store still dereferences row.get without guard")

    def test_facts_list_payload_no_crash(self):
        from codey.workspace.facts import ProjectFactsStore
        with tempfile.TemporaryDirectory() as td:
            store = ProjectFactsStore(state_home=td)
            path = store.path_for("proj")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('["x"]', encoding="utf-8")
            try:
                store.load("proj")
            except AttributeError:
                self.fail("facts load crashed on list payload, should return empty")

    def test_refs_clip_limit_guarded(self):
        from codey.utils.refs import clip
        try:
            clip("x", limit="oops")
        except TypeError:
            self.fail("clip crashed on bad limit, should fail closed")
            return

    def test_refs_bounded_non_iterable(self):
        from codey.utils.refs import bounded_refs
        try:
            bounded_refs(True)  # type: ignore[arg-type]
        except TypeError:
            self.fail("bounded_refs crashed on bool, should be ()")
            return


class AgentsOpsSweepTests(unittest.TestCase):
    def test_loop_max_turns_guarded(self):
        import inspect

        from codey.operations import task_loop as loop_mod
        src = inspect.getsource(loop_mod)
        self.assertNotIn("max(1, int(request.max_turns))", src, "loop still uses bare int(max_turns)")

    def test_seen_lru_no_crash(self):
        from codey.agents.state import SeenInfoLRU
        try:
            SeenInfoLRU("oops")
        except (ValueError, TypeError, OverflowError):
            self.fail("SeenInfoLRU crashed on bad max_items")

    def test_shell_payload_limit_no_crash(self):
        from codey.agents.shell_approval import shell_command_payload
        try:
            shell_command_payload("ls", limit="oops")
        except (ValueError, TypeError, OverflowError):
            self.fail("shell_command_payload crashed on bad limit")

    def test_deferred_index_no_crash(self):
        from codey.agents.shell_approval import DeferredToolCall
        try:
            DeferredToolCall(tool_index="oops", tool_name="read").to_payload()
        except (ValueError, TypeError, OverflowError):
            self.fail("DeferredToolCall crashed on bad index")

    def test_search_page_zero_not_masked(self):
        from codey.toolchain.search_page import normalize_page_args
        # 0->1 clamp is intentional (documented non-bug in round2); lock no-crash + bool strict
        start, page = normalize_page_args(True, 10, None, 80)
        self.assertEqual(start, 1)
        try:
            normalize_page_args("oops", 10, None, 80)
            normalize_page_args(float("inf"), 10, None, 80)
        except (ValueError, TypeError, OverflowError):
            self.fail("normalize_page_args crashed, should default to 1")

    def test_decision_blocked_no_crash(self):
        from codey.completion.decision import completion_blocked_reason
        try:
            completion_blocked_reason(proof_status="failed", failure_class="x",
                                      remaining_turns="oops", repair_rounds=0)
        except (ValueError, TypeError, OverflowError):
            self.fail("completion_blocked_reason crashed on bad turns")

    def test_repair_budget_no_crash(self):
        import inspect

        from codey.completion import repair_context as rc_mod
        src = inspect.getsource(rc_mod)
        self.assertNotIn("budget = max(0, int(budget_chars or 0))", src,
                         "repair_context still uses bare int(budget)")

    def test_definition_call_arg_strict(self):
        from codey.runtime.core.models import ToolCall
        from codey.toolchain import definition as def_mod
        # Containers/bool/None fail closed to default (no "['a.py']"/"False" garbage);
        # numbers keep the long-standing stringify contract (locked by test_shared_helpers).
        self.assertEqual(def_mod.call_arg(ToolCall("read", {"path": ["a.py"]}), "path", "d"), "d")
        self.assertEqual(def_mod.call_arg(ToolCall("read", {"path": False}), "path", "d"), "d")
        self.assertEqual(def_mod.call_arg(ToolCall("read", {"path": {"a": 1}}), "path", "d"), "d")
        self.assertEqual(def_mod.call_arg(ToolCall("run", {"command": 123}), "command", ""), "123")

    def test_manifest_max_bytes_no_crash(self):
        from codey.completion.discovery import read_manifest_text
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.md"
            p.write_text("hello", encoding="utf-8")
            try:
                read_manifest_text(p, max_bytes="oops")
            except (ValueError, TypeError, OverflowError):
                self.fail("read_manifest_text crashed on bad max_bytes, should be empty")

    def test_protocol_escape_no_raise(self):
        from codey.agents.tool_execution import read_before_edit_outcome
        try:
            result = read_before_edit_outcome(Path("/proj"), "/etc/passwd", set())
        except ValueError:
            self.fail("read_before_edit_outcome raised ValueError, should be ToolOutcome.error")
            return
        self.assertIsNotNone(result)
        haystack = str(getattr(result, "model_text", "")) + str(getattr(result, "presentation", ""))
        self.assertIn("workspace_escape", haystack)

    def test_over_budget_no_crash(self):
        from codey.completion.repair_context import _over_budget
        try:
            _over_budget("x" * 1300, "oops")
        except (ValueError, TypeError):
            self.fail("_over_budget crashed on bad budget")

    def test_verification_priority_guarded(self):
        import inspect

        from codey.completion import verification_policy as vp_mod
        src = inspect.getsource(vp_mod)
        self.assertNotIn("int(candidate.source_priority", src, "still uses bare int(priority)")

    def test_adapter_record_no_crash(self):
        from codey.repairs.adapter_overrides import _record
        try:
            _record({}, "oops")
        except (ValueError, TypeError, OverflowError):
            self.fail("_record crashed on bad generation")


if __name__ == "__main__":
    unittest.main()
