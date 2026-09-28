"""Golden behavior locks for the trace.py split.

These tests pin the observable behavior that the split must not change:

1. ``test_kitchen_sink_payload_matches_golden`` — one run containing
   Research + Completion + Protocol records produces byte-identical
   canonical JSON before and after the split (fixture generated
   pre-split;落盘 uses sort_keys so bytes compare too).
2. ``test_payload_carries_no_raw_text`` — no raw prompt / webpage body /
   evidence prose / provider error text lands in the payload.
3. ``test_store_open_survives_write_failure`` — a failing Trace write
   disables the recorder without raising; task/ledger flow never sees it.
4. ``test_bad_epoch_writes_no_row_and_consumes_no_key`` — topic continuity
   and repair context without a valid epoch write no row and leave the
   digest admissible for a later valid send.

All four pass before the split and must pass after it.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "trace_split_golden.json"

HEX16 = "0123456789abcdef"
DIGEST = "sha256:" + "ab" * 32
EPOCH = "ctx_epoch:" + HEX16

SECRET_PROMPT = "sk-live-trace-golden-secret-must-never-land"
SECRET_PAGE = "webpage body bytes that must never land in trace"
SECRET_ERROR = "provider raw error text that must never land"


def _recorder(tmp: str):
    from codey.runs.trace import RunTraceStore

    store = RunTraceStore(tmp)
    return store.open(
        run_id="run-golden",
        session_id="sess-golden",
        project=None,
        mode_initial="auto",
        provider_initial="deepseek",
    )


def drive_kitchen_sink(rec):
    rec.record_mode_selection(
        baseline_mode="auto",
        selected_mode="project",
        final_mode="project",
        source="user",
        reason_code="explicit",
    )
    rec.record_permission_profile("default", phase="start")
    rec.record_tool_contract_hash("model-hash", phase="coding")
    rec.record_runtime_tool_contract_hash("runtime-hash", phase="coding")
    rec.record_prompt_section(
        "task",
        "do the thing " + SECRET_PROMPT,
        purpose="task",
        epoch_id=EPOCH,
        admission_reason="user_task",
        capability_id="coding",
    )
    rec.record_prompt_surface({
        # surface_id must equal the canonical derivation for
        # (phase=coding, send_ref=send-1, digest=ab*32).
        "schema_version": 1,
        "surface_id": "prompt_surface:6e277a49112cc035",
        "phase": "coding",
        "prompt_digest": DIGEST,
        "prompt_chars": 12,
        "epoch_id": EPOCH,
        "send_ref": "send-1",
    })
    rec.record_context_sources([], epoch_id=EPOCH)
    rec.record_local_context_refs([{"id": "note-1", "scope": "session", "kind": "note"}])
    rec.record_research_notes(["note-1"])
    rec.record_research_sources([{
        "requested_url": "https://example.com/a",
        "title": "Example",
    }])
    rec.record_research_record_summary({
        "record_id": "research_record:" + HEX16,
        "record_digest": DIGEST,
        "answer_status": "answered",
        "source_count": 2,
        "evidence_count": 1,
        "claim_count": 1,
        "assumption_count": 0,
        "unsupported_claim_count": 0,
    })
    rec.record_evidence_ledger_write({
        "record_id": "research_record:" + HEX16,
        "ledger_ref": "evidence_ledger:" + HEX16,
        "ok": True,
        "counts": {"records": 1, "sources": 2},
    })
    rec.record_research_proof_review({
        "proof_ref": "research_proof:" + HEX16,
        "ok": True,
        "record_id": "research_record:" + HEX16,
        "record_digest": DIGEST,
        "question_digest": DIGEST,
        "answer_status": "answered",
        "reason_codes": ["evidence_backed"],
    })
    rec.record_research_plan({
        "plan_ref": "research_plan:" + HEX16,
        "max_depth": 2,
        "max_queries": 3,
        "max_sources": 4,
        "source_preferences": ["arxiv"],
    })
    rec.record_research_pipeline_result({"stop_reason": "done", "followup_rounds": 1})
    rec.record_research_connector_errors([{
        "connector_id": "arxiv",
        "action": "search",
        "error": "timeout",
        "count": 2,
    }])
    rec.record_research_done_compilation({"reason": "done", "source_count": 3})
    rec.record_analysis_run({
        "analysis_run_id": "analysis_run:" + HEX16,
        "tool_id": "3:0",
        "tool_name": "run",
        "command_digest": DIGEST,
        "command_display": "echo hi",
        "ok": True,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "exit_code": 0,
    })
    rec.record_artifact_refs([{
        "artifact_id": "artifact:" + HEX16,
        "version_id": "artifact_version:" + HEX16,
        "sha256": "ab" * 32,
        "size": 10,
    }])
    rec.record_reproducibility_capsule({
        "capsule_id": "capsule:" + HEX16,
        "analysis_run_refs": ["analysis_run:" + HEX16],
        "artifact_refs": ["artifact_version:" + HEX16],
        "environment_digest": DIGEST,
        "reproduction_status": "reproduced",
    })
    rec.record_review_findings([{
        "finding_id": "review_finding:" + HEX16,
        "kind": "unsupported_claim",
        "severity": "warning",
        "target_ref": "claim:" + HEX16,
        "reason_codes": ["needs_evidence"],
    }])
    rec.record_planner_gaps([{
        "gap_id": "planner_gap:" + HEX16,
        "gap_kind": "followup_search",
        "target_ref": "claim:" + HEX16,
    }])
    rec.record_research_source_trust([{
        "source_ref": "source:" + HEX16,
        "source_class": "official",
        "tier": 1,
    }])
    rec.record_research_brief_projection({
        "record_ref": "research_record:" + HEX16,
        "record_digest": DIGEST,
        "answer_status": "answered",
        "profile_id": "default",
        "claim_refs": ["claim:" + HEX16],
        "claims": [{
            "claim_ref": "claim:" + HEX16,
            "status": "evidence_backed",
            "text": "bounded claim prose " + SECRET_PAGE,
            "evidence_count": 1,
        }],
        "counts": {"claims": 1},
    })
    rec.record_research_topic_continuity(
        {
            "admitted": True,
            "schema_version": 1,
            "digest": DIGEST,
            "context_source": "research",
            "item_count": 1,
            "items": [{"refs": ["claim:" + HEX16], "kind": "claim"}],
        },
        epoch_id=EPOCH,
    )
    rec.record_completion_repair_context(
        {
            "admitted": True,
            "schema_version": 1,
            "digest": DIGEST,
            "context_source": "completion",
            "failure_class": "test_failure",
            "proof_id": "completion_proof:" + HEX16,
            "contract_id": "completion_contract:" + HEX16,
        },
        epoch_id=EPOCH,
    )
    rec.record_completion_proof({
        "proof_id": "completion_proof:" + HEX16,
        "contract_id": "completion_contract:" + HEX16,
        "domain": "coding",
        "status": "complete",
        "checks": [{"check_id": "tests_pass", "status": "pass"}],
    })
    rec.record_edit_integrity({
        "observation_ref": "edit_integrity:" + HEX16,
        "status": "clean",
        "severity": "low",
        "affected_paths": ["app.py"],
    })
    rec.record_protocol_codec("json", phase="coding")
    rec.record_protocol_error("unknown_tool", phase="coding", tool_name="mystery_tool")
    rec.record_protocol_repair_prompt("unknown_tool", phase="coding")
    rec.record_protocol_valid_turn(3, phase="coding")
    rec.record_fallback(
        from_provider="deepseek", to_provider="mimo",
        phase="coding", reason_code="timeout",
    )
    from types import SimpleNamespace

    rec.record_provider_failure("deepseek", SimpleNamespace(
        action="send", kind="timeout", stage="chat"))
    rec.record_policy_decision({
        "kind": "shell",
        "decision": "ask_user",
        "guard_id": "shell-approval",
        "reason_code": "untrusted_command",
        "phase": "coding",
        "subject_ref": "action:" + "cd" * 32,
        "display_digest": DIGEST,
        "display_chars": 10,
    })
    rec.finish(status="done", mode="project", provider="mimo")


class TraceSplitGoldenTests(unittest.TestCase):
    def test_kitchen_sink_payload_matches_golden(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            rec = _recorder(td)
            drive_kitchen_sink(rec)
            payload = rec.manifest.to_payload()
        text = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        self.assertTrue(FIXTURE.exists(), "golden fixture missing; generate pre-split")
        self.assertEqual(text, FIXTURE.read_text(encoding="utf-8"))

    def test_payload_carries_no_raw_text(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            rec = _recorder(td)
            drive_kitchen_sink(rec)
            payload = rec.manifest.to_payload()
        text = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        for secret in (SECRET_PROMPT, SECRET_PAGE, SECRET_ERROR):
            self.assertNotIn(secret, text)

    def test_store_open_survives_write_failure(self):
        import tempfile
        from unittest import mock

        from codey.runs.trace import RunTraceStore

        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.runs.trace.write_json_atomic", side_effect=OSError("disk")
        ):
            rec = RunTraceStore(td).open(
                run_id="r1",
                session_id="s1",
                project=None,
                mode_initial="auto",
                provider_initial="deepseek",
            )
            self.assertTrue(rec.disabled)
            # Task/ledger flow never sees the failure: no raise.
            rec.record_protocol_valid_turn(1, phase="coding")
            rec.record_research_notes(["note-1"])
            rec.finish(status="done")

    def test_bad_epoch_writes_no_row_and_consumes_no_key(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            rec = _recorder(td)
            topic = {
                "admitted": True,
                "schema_version": 1,
                "digest": DIGEST,
                "context_source": "research",
            }
            repair = {
                "admitted": True,
                "schema_version": 1,
                "digest": DIGEST,
                "context_source": "completion",
            }
            rec.record_research_topic_continuity(topic, epoch_id="bad-epoch")
            rec.record_completion_repair_context(repair, epoch_id="")
            self.assertEqual(rec.manifest.research_topic_continuity, [])
            self.assertEqual(rec.manifest.completion_repair_context, [])
            rec.record_research_topic_continuity(topic, epoch_id=EPOCH)
            rec.record_completion_repair_context(repair, epoch_id=EPOCH)
            self.assertEqual(len(rec.manifest.research_topic_continuity), 1)
            self.assertEqual(len(rec.manifest.completion_repair_context), 1)


if __name__ == "__main__":
    unittest.main()
