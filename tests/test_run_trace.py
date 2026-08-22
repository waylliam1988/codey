from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import agent
from codey.context_source import (
    ContextSource,
    render_context_sources,
    render_context_sources_with_metadata,
)
from codey.research.controller import controller_action_contract_hash
from codey.research.tool_contract import research_tool_contract_hash
from codey.run_trace import CHECKPOINT_FLUSH_INTERVAL, RunTraceStore
from codey.action_policy import ActionSubject, evaluate_action
from codey.tool_definition import (
    definitions_for_tool_names,
    model_tool_contract_hash,
)


class _PromptProvider:
    name = "Prompt Provider"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def new_chat(self) -> None:
        return None

    def send(self, text: str) -> str:
        self.prompts.append(text)
        return '{"tool":"done","args":{"summary":"ok"}}'


class _NoopTrace:
    def __getattr__(self, _name):
        def call(*_args, **_kwargs):
            return None
        return call


class RunTraceStoreTests(unittest.TestCase):
    def test_path_for_keeps_session_and_run_inside_state_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            path = store.path_for("../session", "../../run")

            self.assertEqual(path.parent.parent, Path(td) / "run_traces")
            self.assertNotIn("..", path.name)
            self.assertTrue(path.name.endswith(".json"))

    def test_prompt_section_records_digest_not_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-1",
                session_id="session-1",
                project=Path(td) / "project",
                mode_initial="project",
                provider_initial="deepseek",
            )
            secret = "SECRET_PROMPT_SHOULD_NOT_BE_SAVED"
            recorder.record_prompt_section(
                "project_map",
                secret,
                purpose="bounded project map",
                budget=120,
                freshness="run_start",
                source_refs=("context_source:project_map",),
            )
            recorder.record_provider_failure(
                "deepseek",
                type("Failure", (), {
                    "action": "send",
                    "kind": "response_missing",
                    "stage": "completion",
                    "message": "RAW_PROVIDER_ERROR_SHOULD_NOT_BE_SAVED",
                })(),
            )
            recorder.finish(status="done", mode="project", provider="deepseek")

            payload = json.loads(store.path_for("session-1", "run-1").read_text(encoding="utf-8"))
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["kind"], "run_trace_manifest")
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["prompt_sections"][0]["name"], "project_map")
            self.assertEqual(payload["prompt_sections"][0]["chars"], len(secret))
            self.assertEqual(payload["prompt_sections"][0]["purpose"], "bounded project map")
            self.assertTrue(payload["prompt_sections"][0]["model_visible"])
            self.assertIn("digest", payload["prompt_sections"][0])
            self.assertNotIn(secret, serialized)
            self.assertNotIn("RAW_PROVIDER_ERROR_SHOULD_NOT_BE_SAVED", serialized)

    def test_write_failure_disables_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            with mock.patch("codey.run_trace.write_json_atomic", side_effect=OSError("disk")):
                recorder = store.open(
                    run_id="run-fail",
                    session_id="session-fail",
                    project=None,
                    mode_initial="chat",
                    provider_initial="deepseek",
                )
                recorder.record_prompt_section("chat", "hello")

            self.assertTrue(recorder.disabled)

    def test_string_refs_are_treated_as_one_ref(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-string-ref",
                session_id="session-string-ref",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_notes("note-single")
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for("session-string-ref", "run-string-ref").read_text(
                    encoding="utf-8",
                )
            )

            self.assertEqual(payload["research_note_ids"], ["note-single"])

    def test_model_visible_prompt_section_gets_source_ref_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-source-fallback",
                session_id="session-source-fallback",
                project=None,
                mode_initial="chat",
                provider_initial="deepseek",
            )
            recorder.record_prompt_section("Task Section", "hello")
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-source-fallback",
                    "run-source-fallback",
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(
                payload["prompt_sections"][0]["source_refs"],
                ["prompt_section:Task_Section"],
            )

    def test_source_refs_do_not_store_url_userinfo_or_port_in_host(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-source-ref",
                session_id="session-source-ref",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_sources(({
                "requested_url": "https://user:token@example.com:8443/request",
                "final_url": "https://user:token@example.com:8443/final",
                "title": "Secret Source Title",
            },))
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for("session-source-ref", "run-source-ref").read_text(
                    encoding="utf-8",
                )
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["research_source_refs"][0]["host"], "example.com")
            self.assertNotIn("user", serialized)
            self.assertNotIn("token", serialized)
            self.assertNotIn("8443", serialized)
            self.assertNotIn("Secret Source Title", serialized)

    def test_research_record_summary_is_bounded_and_digest_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-research-record",
                session_id="session-research-record",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_record_summary({
                "record_id": "research_record:" + "a" * 16,
                "answer_status": "answered",
                "source_count": 2,
                "evidence_count": 3,
                "claim_count": 4,
                "assumption_count": 1,
                "unsupported_claim_count": 0,
                "record_digest": "sha256:" + "a" * 64,
                "summary": "SECRET_REPORT_TEXT_SHOULD_NOT_BE_SAVED",
                "final_url": "https://example.com/secret",
            })
            recorder.record_research_record_summary({
                "record_id": "research_record:" + "a" * 16,
                "answer_status": "answered",
                "source_count": 99,
                "record_digest": "sha256:" + "a" * 64,
            })
            recorder.record_research_record_summary({
                "record_id": "research_record:" + "b" * 16,
                "answer_status": "answered",
                "record_digest": "not-a-digest",
            })
            recorder.record_research_record_summary({
                "record_id": "SECRET_QUESTION_ABOUT_ACME_TOKEN",
                "answer_status": "answered",
                "source_count": 1,
                "record_digest": "sha256:" + "c" * 64,
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-research-record",
                    "run-research-record",
                ).read_text(encoding="utf-8")
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["research_records"], [{
                "record_id": "research_record:" + "a" * 16,
                "answer_status": "answered",
                "source_count": 2,
                "evidence_count": 3,
                "claim_count": 4,
                "assumption_count": 1,
                "unsupported_claim_count": 0,
                "record_digest": "sha256:" + "a" * 64,
            }])
            self.assertNotIn("SECRET_REPORT_TEXT_SHOULD_NOT_BE_SAVED", serialized)
            self.assertNotIn("https://example.com/secret", serialized)
            self.assertNotIn("SECRET_QUESTION_ABOUT_ACME_TOKEN", serialized)

    def test_evidence_ledger_write_trace_is_bounded_and_ref_shaped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-evidence-ledger",
                session_id="session-evidence-ledger",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_evidence_ledger_write({
                "ok": True,
                "skipped": False,
                "reason_code": "written",
                "ledger_ref": "evidence_ledger:" + "a" * 16,
                "record_id": "research_record:" + "b" * 16,
                "counts": {
                    "records": 1,
                    "sources": 2,
                    "evidence": 3,
                    "claims": 4,
                    "assumptions": 5,
                    "relations": 6,
                    "raw_secret_count": 999,
                },
                "warnings": [
                    "bounded-warning",
                    "warning with spaces",
                ],
                "raw_url": "https://example.com/SECRET_URL",
            })
            recorder.record_evidence_ledger_write({
                "ok": True,
                "ledger_ref": "not-a-ledger-ref",
                "record_id": "SECRET_QUESTION_ABOUT_ACME_TOKEN",
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-evidence-ledger",
                    "run-evidence-ledger",
                ).read_text(encoding="utf-8")
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["research_evidence_ledgers"], [{
                "ok": True,
                "skipped": False,
                "reason_code": "written",
                "ledger_ref": "evidence_ledger:" + "a" * 16,
                "record_id": "research_record:" + "b" * 16,
                "counts": {
                    "records": 1,
                    "sources": 2,
                    "evidence": 3,
                    "claims": 4,
                    "assumptions": 5,
                    "relations": 6,
                },
                "warnings": [
                    "bounded-warning",
                    "warning_with_spaces",
                ],
            }])
            self.assertNotIn("https://example.com/SECRET_URL", serialized)
            self.assertNotIn("SECRET_QUESTION_ABOUT_ACME_TOKEN", serialized)

    def test_evidence_ledger_write_trace_filters_sensitive_codes_and_malformed_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-evidence-ledger-sensitive",
                session_id="session-evidence-ledger-sensitive",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_evidence_ledger_write({
                "ok": True,
                "ledger_ref": "evidence_ledger:" + "a" * 16,
                "record_id": "research_record:" + "b" * 16,
                "reason_code": "SECRET_CLIENT_NAME",
                "warnings": "SECRET_LEDGER_WARNING",
            })
            recorder.record_evidence_ledger_write({
                "ok": True,
                "ledger_ref": "evidence_ledger:" + "c" * 16,
                "record_id": "research_record:" + "d" * 16,
                "reason_code": "token_budget_exceeded",
                "warnings": ["kept-warning", "authorization_required", "SECRET_LEDGER_WARNING", "密码"],
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-evidence-ledger-sensitive",
                    "run-evidence-ledger-sensitive",
                ).read_text(encoding="utf-8")
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["research_evidence_ledgers"][0]["reason_code"], "")
            self.assertNotIn("warnings", payload["research_evidence_ledgers"][0])
            self.assertEqual(payload["research_evidence_ledgers"][1]["reason_code"], "token_budget_exceeded")
            self.assertEqual(
                payload["research_evidence_ledgers"][1]["warnings"],
                ["kept-warning", "authorization_required"],
            )
            self.assertNotIn("SECRET_CLIENT_NAME", serialized)
            self.assertNotIn("SECRET_LEDGER_WARNING", serialized)
            self.assertNotIn("密码", serialized)

    def test_research_proof_review_trace_is_bounded_and_ref_shaped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-research-proof",
                session_id="session-research-proof",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_proof_review({
                "proof_ref": "research_proof:" + "a" * 16,
                "record_id": "research_record:" + "b" * 16,
                "record_digest": "sha256:" + "c" * 64,
                "question_digest": "sha256:" + "e" * 64,
                "ok": False,
                "answers_question": False,
                "answer_status": "partial",
                "answer_coverage_score": 1.5,
                "gap_count": 3,
                "warning_count": 4,
                "planner_signal_count": 5,
                "reason_codes": ["claim_missing_support_relation", "bad reason with spaces"],
                "followup_questions": ["SECRET_FOLLOWUP_SHOULD_NOT_BE_SAVED"],
                "raw_url": "https://example.com/SECRET_URL",
            })
            recorder.record_research_proof_review({
                "proof_ref": "research_proof:" + "a" * 16,
                "record_id": "research_record:" + "b" * 16,
                "record_digest": "sha256:" + "c" * 64,
                "question_digest": "sha256:" + "e" * 64,
                "ok": False,
                "answers_question": False,
                "answer_status": "partial",
                "answer_coverage_score": 1.0,
                "reason_codes": ["claim_missing_support_relation", "bad reason with spaces"],
            })
            recorder.record_research_proof_review({
                "proof_ref": "research_proof:abc123",
                "record_id": "SECRET_QUESTION_ABOUT_ACME_TOKEN",
                "record_digest": "sha256:" + "d" * 64,
            })
            recorder.record_research_proof_review({
                "proof_ref": "research_proof:" + "f" * 16,
                "question_digest": "sha256:" + "1" * 64,
                "ok": False,
                "answers_question": False,
                "answer_status": "not_answered",
                "answer_coverage_score": 0,
                "reason_codes": ["missing_research_record"],
                "record_id": "SECRET_MISSING_RECORD_ID",
                "record_digest": "not-a-digest",
            })
            recorder.record_research_proof_review({
                "proof_ref": "research_proof:" + "2" * 16,
                "question_digest": "sha256:" + "3" * 64,
                "ok": True,
                "answers_question": True,
                "answer_status": "answered",
                "record_id": "SECRET_OK_MISSING_RECORD_ID",
                "record_digest": "not-a-digest",
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-research-proof",
                    "run-research-proof",
                ).read_text(encoding="utf-8")
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(
                payload["research_proof_reviews"],
                [
                    {
                        "proof_ref": "research_proof:" + "a" * 16,
                        "ok": False,
                        "answers_question": False,
                        "answer_status": "partial",
                        "answer_coverage_score": 1.0,
                        "gap_count": 3,
                        "warning_count": 4,
                        "planner_signal_count": 5,
                        "reason_codes": [
                            "claim_missing_support_relation",
                            "bad_reason_with_spaces",
                        ],
                        "record_id": "research_record:" + "b" * 16,
                        "record_digest": "sha256:" + "c" * 64,
                        "question_digest": "sha256:" + "e" * 64,
                    },
                    {
                        "proof_ref": "research_proof:" + "f" * 16,
                        "ok": False,
                        "answers_question": False,
                        "answer_status": "not_answered",
                        "answer_coverage_score": 0.0,
                        "gap_count": 0,
                        "warning_count": 0,
                        "planner_signal_count": 0,
                        "reason_codes": ["missing_research_record"],
                        "question_digest": "sha256:" + "1" * 64,
                    },
                ],
            )
            self.assertNotIn("SECRET_FOLLOWUP_SHOULD_NOT_BE_SAVED", serialized)
            self.assertNotIn("https://example.com/SECRET_URL", serialized)
            self.assertNotIn("SECRET_QUESTION_ABOUT_ACME_TOKEN", serialized)
            self.assertNotIn("SECRET_MISSING_RECORD_ID", serialized)
            self.assertNotIn("SECRET_OK_MISSING_RECORD_ID", serialized)

    def test_research_proof_review_trace_filters_sensitive_and_malformed_reason_codes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-research-proof-sensitive",
                session_id="session-research-proof-sensitive",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_proof_review({
                "proof_ref": "research_proof:" + "a" * 16,
                "ok": False,
                "reason_codes": "SECRET_REASON",
            })
            recorder.record_research_proof_review({
                "proof_ref": "research_proof:" + "b" * 16,
                "ok": False,
                "reason_codes": ["valid_code", "authorization_required", "SECRET_CLIENT_NAME"],
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-research-proof-sensitive",
                    "run-research-proof-sensitive",
                ).read_text(encoding="utf-8")
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["research_proof_reviews"][0]["reason_codes"], [])
            self.assertEqual(
                payload["research_proof_reviews"][1]["reason_codes"],
                ["valid_code", "authorization_required"],
            )
            self.assertNotIn("SECRET_REASON", serialized)
            self.assertNotIn("SECRET_CLIENT_NAME", serialized)

    def test_research_plan_trace_is_bounded_dry_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-research-plan",
                session_id="session-research-plan",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_plan({
                "plan_ref": "research_plan:" + "a" * 16,
                "question_digest": "sha256:" + "b" * 64,
                "proof_ref": "research_proof:" + "c" * 16,
                "dry_run": True,
                "max_depth": 99,
                "max_queries": 99,
                "max_sources": 99,
                "query_count": 99,
                "source_preferences": [
                    "pubmed",
                    "bad preference with spaces",
                    "SECRET_CLIENT_NAME",
                ],
                "reason_codes": [
                    "coverage_gap",
                    "bad reason with spaces",
                    "answer_status_insufficient_evidence",
                    "SECRET_CLIENT_NAME",
                    "sk-" + "a" * 24,
                ],
                "warnings": [
                    "rss_optional",
                    "warning with spaces",
                    "record_pruned_for_ledger_closure",
                    "SECRET_CLIENT_NAME",
                    "ghp_" + "b" * 24,
                ],
                "query_preview": "SECRET_QUERY_SHOULD_NOT_BE_SAVED",
                "raw_url": "https://example.com/SECRET_URL",
            })
            recorder.record_research_plan({
                "plan_ref": "research_plan:" + "a" * 16,
                "question_digest": "sha256:" + "b" * 64,
            })
            recorder.record_research_plan({
                "plan_ref": "SECRET_PLAN_REF",
                "question_digest": "sha256:" + "d" * 64,
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-research-plan",
                    "run-research-plan",
                ).read_text(encoding="utf-8")
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["research_plans"], [{
                "plan_ref": "research_plan:" + "a" * 16,
                "dry_run": True,
                "max_depth": 1,
                "max_queries": 8,
                "max_sources": 12,
                "query_count": 8,
                "source_preferences": ["pubmed"],
                "reason_codes": [
                    "coverage_gap",
                    "bad_reason_with_spaces",
                    "answer_status_insufficient_evidence",
                ],
                "warnings": [
                    "rss_optional",
                    "warning_with_spaces",
                    "record_pruned_for_ledger_closure",
                ],
                "question_digest": "sha256:" + "b" * 64,
                "proof_ref": "research_proof:" + "c" * 16,
            }])
            self.assertNotIn("SECRET_QUERY_SHOULD_NOT_BE_SAVED", serialized)
            self.assertNotIn("SECRET_CLIENT_NAME", serialized)
            self.assertNotIn("sk-", serialized)
            self.assertNotIn("ghp_", serialized)
            self.assertNotIn("https://example.com/SECRET_URL", serialized)
            self.assertNotIn("SECRET_PLAN_REF", serialized)

    def test_research_plan_trace_list_fields_ignore_non_collections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-research-plan-shape",
                session_id="session-research-plan-shape",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_plan({
                "plan_ref": "research_plan:" + "a" * 16,
                "source_preferences": "pubmed",
                "reason_codes": None,
                "warnings": "rss_optional",
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-research-plan-shape",
                    "run-research-plan-shape",
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(payload["research_plans"][0]["source_preferences"], [])
            self.assertEqual(payload["research_plans"][0]["reason_codes"], [])
            self.assertEqual(payload["research_plans"][0]["warnings"], [])

    def test_research_pipeline_result_trace_is_bounded_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-research-pipeline",
                session_id="session-research-pipeline",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_pipeline_result({
                "followup_applied": True,
                "followup_rounds": 99,
                "stop_reason": "done",
                "planner_stop_reason": "followup_iteration_error",
                "attempted_fresh_source_count": 2,
                "attempted_new_evidence_count": 3,
                "raw_query": "SECRET_QUERY_SHOULD_NOT_BE_SAVED",
                "raw_url": "https://example.com/secret",
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-research-pipeline",
                    "run-research-pipeline",
                ).read_text(encoding="utf-8")
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["research_pipeline_runs"], [{
                "followup_applied": True,
                "followup_rounds": 3,
                "stop_reason": "done",
                "planner_stop_reason": "followup_iteration_error",
                "fresh_source_count": 0,
                "new_evidence_count": 0,
                "final_evidence_count": 0,
                "attempted_fresh_source_count": 2,
                "attempted_new_evidence_count": 3,
            }])


            self.assertNotIn("SECRET_QUERY_SHOULD_NOT_BE_SAVED", serialized)
            self.assertNotIn("https://example.com/secret", serialized)


    def test_research_connector_errors_are_bounded_trace_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-research-connector-errors",
                session_id="session-research-connector-errors",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_connector_errors([
                {
                    "connector_id": "pubmed",
                    "action": "fetch_lookup",
                    "error": "ValueError",
                },
                {
                    "connector_id": "pubmed",
                    "action": "fetch_lookup",
                    "error": "ValueError",
                },
                {
                    "connector_id": "connector",
                    "action": "search",
                    "error": "connector_query_sensitive_skipped",
                },
                {
                    "connector_id": "SECRET_CLIENT_NAME",
                    "action": "search",
                    "error": "ValueError",
                },
                {
                    "connector_id": "arxiv",
                    "action": "https://example.com/SECRET_URL",
                    "error": "sk-" + "a" * 24,
                },
            ])
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-research-connector-errors",
                    "run-research-connector-errors",
                ).read_text(encoding="utf-8")
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["research_connector_errors"], [
                {
                    "connector_id": "pubmed",
                    "action": "fetch_lookup",
                    "error": "ValueError",
                    "count": 2,
                },
                {
                    "connector_id": "connector",
                    "action": "search",
                    "error": "connector_query_sensitive_skipped",
                    "count": 1,
                },
            ])
            self.assertNotIn("SECRET_CLIENT_NAME", serialized)
            self.assertNotIn("https://example.com/SECRET_URL", serialized)
            self.assertNotIn("sk-", serialized)

    def test_research_done_compilation_is_bounded_trace_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-done-compiler",
                session_id="session-done-compiler",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_done_compilation({
                "reason": "compiled_citations",
                "source_count": 2,
                "answer": "RAW_ANSWER_SHOULD_NOT_BE_SAVED",
            })
            recorder.record_research_done_compilation({
                "reason": "client_secret",
                "source_count": 1,
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-done-compiler",
                    "run-done-compiler",
                ).read_text(encoding="utf-8")
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["research_done_compilations"], [
                {"reason": "compiled_citations", "source_count": 2},
            ])
            self.assertNotIn("RAW_ANSWER_SHOULD_NOT_BE_SAVED", serialized)
            self.assertNotIn("client_secret", serialized)

    def test_policy_decision_records_digest_without_raw_display(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-policy",
                session_id="session-policy",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            secret_command = "python -m pip install secret-package"
            decision = evaluate_action(ActionSubject(
                "run_command",
                phase="writer",
                permission_profile="coding_writer",
                project=td,
                path=".",
                command=secret_command,
                tool_name="run",
            ))

            recorder.record_policy_decision(decision)
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for("session-policy", "run-policy").read_text(
                    encoding="utf-8",
                )
            )
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["policy_decisions"][0]["kind"], "run_command")
            self.assertEqual(payload["policy_decisions"][0]["decision"], "deny")
            self.assertEqual(
                payload["policy_decisions"][0]["reason_code"],
                "command_not_allowed",
            )
            self.assertIn("subject_ref", payload["policy_decisions"][0])
            self.assertIn("display_digest", payload["policy_decisions"][0])
            self.assertNotIn(secret_command, serialized)
            self.assertNotIn("secret-package", serialized)

    def test_policy_decision_mapping_requires_digest_shaped_refs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-policy-mapping",
                session_id="session-policy-mapping",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )

            recorder.record_policy_decision({
                "kind": "run_command",
                "decision": "deny",
                "guard_id": "run_command_guard",
                "reason_code": "command_not_allowed",
                "phase": "writer",
                "subject_ref": "rm -rf /",
                "display_digest": "secret command",
            })
            recorder.record_policy_decision({
                "kind": "run_command",
                "decision": "deny",
                "guard_id": "run_command_guard",
                "reason_code": "command_not_allowed",
                "phase": "writer",
                "subject_ref": "action:" + ("a" * 64),
                "display_digest": "sha256:" + ("b" * 64),
                "display_chars": 12,
            })
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for("session-policy-mapping", "run-policy-mapping").read_text(
                    encoding="utf-8",
                )
            )

        self.assertEqual(len(payload["policy_decisions"]), 1)
        self.assertEqual(payload["policy_decisions"][0]["subject_ref"], "action:" + ("a" * 64))
        self.assertEqual(payload["policy_decisions"][0]["display_digest"], "sha256:" + ("b" * 64))

    def test_prompt_section_records_use_checkpoint_flush(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            with mock.patch("codey.run_trace.write_json_atomic") as write:
                recorder = store.open(
                    run_id="run-batch",
                    session_id="session-batch",
                    project=None,
                    mode_initial="project",
                    provider_initial="deepseek",
                )

                for index in range(CHECKPOINT_FLUSH_INTERVAL - 1):
                    recorder.record_prompt_section(f"section-{index}", f"text {index}")

                self.assertEqual(write.call_count, 1)

                recorder.record_prompt_section(
                    f"section-{CHECKPOINT_FLUSH_INTERVAL}",
                    "flush now",
                )

                self.assertEqual(write.call_count, 2)

    def test_provider_send_prompt_sections_flush_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            with mock.patch("codey.run_trace.write_json_atomic") as write:
                recorder = store.open(
                    run_id="run-model-boundary",
                    session_id="session-model-boundary",
                    project=None,
                    mode_initial="project",
                    provider_initial="deepseek",
                )

                recorder.record_prompt_section(
                    "context_metadata",
                    "not model boundary yet",
                    freshness="run_start",
                )
                self.assertEqual(write.call_count, 1)

                recorder.record_prompt_section(
                    "coding_outbound_prompt",
                    "visible to provider",
                    freshness="provider_send",
                )
                self.assertEqual(write.call_count, 2)

                recorder.record_prompt_section(
                    "review_task",
                    "prepared for secondary workflow",
                    freshness="secondary_input_prepared",
                )
                self.assertEqual(write.call_count, 2)

                recorder.record_prompt_section(
                    "review_prompt",
                    "visible to provider",
                    freshness="provider_send",
                )
                self.assertEqual(write.call_count, 3)

    def test_prompt_section_dedup_keeps_freshness_and_flushes_duplicate_model_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            with mock.patch("codey.run_trace.write_json_atomic") as write:
                recorder = store.open(
                    run_id="run-freshness-key",
                    session_id="session-freshness-key",
                    project=None,
                    mode_initial="project",
                    provider_initial="deepseek",
                )

                recorder.record_prompt_section(
                    "same_section",
                    "same text",
                    freshness="run_start",
                    source_refs=("same:ref",),
                )
                self.assertEqual(write.call_count, 1)

                recorder.record_prompt_section(
                    "same_section",
                    "same text",
                    freshness="provider_send",
                    source_refs=("same:ref",),
                )
                self.assertEqual(write.call_count, 2)
                payload = write.call_args.args[1]
                self.assertEqual(
                    [item["freshness"] for item in payload["prompt_sections"]],
                    ["run_start", "provider_send"],
                )

                recorder.record_prompt_section(
                    "same_section",
                    "same text",
                    freshness="provider_send",
                    source_refs=("same:ref",),
                )
                self.assertEqual(write.call_count, 3)

    def test_prompt_section_dedup_keeps_distinct_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-purpose-key",
                session_id="session-purpose-key",
                project=None,
                mode_initial="chat",
                provider_initial="deepseek",
            )

            recorder.record_prompt_section(
                "same_section",
                "same text",
                purpose="first purpose",
                freshness="provider_send",
                source_refs=("same:ref",),
            )
            recorder.record_prompt_section(
                "same_section",
                "same text",
                purpose="second purpose",
                freshness="provider_send",
                source_refs=("same:ref",),
            )
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for("session-purpose-key", "run-purpose-key").read_text(
                    encoding="utf-8",
                )
            )

            self.assertEqual(
                [item["purpose"] for item in payload["prompt_sections"]],
                ["first purpose", "second purpose"],
            )

    def test_prompt_section_records_epoch_admission_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-epoch-meta",
                session_id="session-epoch-meta",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            recorder.record_prompt_section(
                "coding_outbound_prompt",
                "prompt body",
                purpose="coding prompt sent to provider",
                freshness="provider_send",
                source_refs=("provider_send:coding",),
                epoch_id="ctx_epoch:" + "a" * 16,
                admission_reason="provider_turn_boundary",
                capability_id="agent_runner",
            )
            recorder.record_prompt_section(
                "prepared_context",
                "earlier context",
                freshness="run_start",
            )
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for("session-epoch-meta", "run-epoch-meta").read_text(
                    encoding="utf-8",
                )
            )

            outbound = payload["prompt_sections"][0]
            self.assertEqual(outbound["epoch_id"], "ctx_epoch:" + "a" * 16)
            self.assertEqual(outbound["admission_reason"], "provider_turn_boundary")
            self.assertEqual(outbound["capability_id"], "agent_runner")
            prepared = payload["prompt_sections"][1]
            self.assertNotIn("epoch_id", prepared)
            self.assertNotIn("admission_reason", prepared)
            self.assertNotIn("capability_id", prepared)

    def test_prompt_section_dedup_distinguishes_epoch_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-epoch-dedup",
                session_id="session-epoch-dedup",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            for epoch in ("ctx_epoch:" + "0" * 16, "ctx_epoch:" + "1" * 16):
                recorder.record_prompt_section(
                    "same_section",
                    "same text",
                    freshness="provider_send",
                    source_refs=("same:ref",),
                    epoch_id=epoch,
                )
            # Same content without an epoch id is still deduplicated against
            # the empty-epoch bucket only.
            recorder.record_prompt_section(
                "same_section",
                "same text",
                freshness="provider_send",
                source_refs=("same:ref",),
            )
            recorder.record_prompt_section(
                "same_section",
                "same text",
                freshness="provider_send",
                source_refs=("same:ref",),
            )
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for("session-epoch-dedup", "run-epoch-dedup").read_text(
                    encoding="utf-8",
                )
            )

            self.assertEqual(
                [item.get("epoch_id", "") for item in payload["prompt_sections"]],
                [
                    "ctx_epoch:" + "0" * 16,
                    "ctx_epoch:" + "1" * 16,
                    "",
                ],
            )

    def test_delete_session_removes_only_that_session_trace_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            a = store.open(
                run_id="run-a",
                session_id="session-a",
                project=None,
                mode_initial="chat",
                provider_initial="deepseek",
            )
            b = store.open(
                run_id="run-b",
                session_id="session-b",
                project=None,
                mode_initial="chat",
                provider_initial="deepseek",
            )
            a.finish(status="done")
            b.finish(status="done")

            store.delete_session("session-a")

            self.assertFalse(store.path_for("session-a", "run-a").exists())
            self.assertTrue(store.path_for("session-b", "run-b").exists())

    def test_delete_session_never_deletes_run_trace_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            root = Path(td) / "run_traces"
            root.mkdir()
            marker = root / "keep.txt"
            marker.write_text("keep", encoding="utf-8")

            with mock.patch.object(store, "session_dir", return_value=root):
                store.delete_session("bad-session")

            self.assertTrue(marker.exists())


class RunTraceMetadataHelperTests(unittest.TestCase):
    def test_context_metadata_helper_preserves_rendered_text(self) -> None:
        sources = (
            ContextSource(
                key="first",
                loader=lambda: "alpha",
                budget=100,
                freshness="run_start",
                why_included="first",
                heading="First:",
            ),
            ContextSource(
                key="empty",
                loader=lambda: "",
                budget=100,
                freshness="run_start",
                why_included="empty",
            ),
            ContextSource(
                key="second",
                loader=lambda: "beta",
                budget=100,
                freshness="after_tool_result",
                why_included="second",
            ),
        )

        baseline = render_context_sources(sources)
        rendered = render_context_sources_with_metadata(sources)

        self.assertEqual(rendered.text, baseline)
        self.assertEqual(rendered.text, "First:\nalpha\n\nbeta")
        self.assertEqual([item.key for item in rendered.sources], ["first", "second"])
        self.assertEqual(rendered.sources[0].budget, 100)
        self.assertEqual(rendered.sources[1].freshness, "after_tool_result")

    def test_context_sources_carry_capability_and_admission_metadata(self) -> None:
        sources = (
            ContextSource(
                key="ghost_directive",
                loader=lambda: "directive body",
                budget=100,
                freshness="run_start",
                why_included="bounded local confirmed Ghost memory",
                capability_id="local_context",
                admission_reason="run_start_assembly",
            ),
            ContextSource(
                key="plain_source",
                loader=lambda: "plain body",
                budget=100,
                freshness="run_start",
                why_included="no metadata source",
            ),
        )
        rendered = render_context_sources_with_metadata(sources)
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-source-meta",
                session_id="session-source-meta",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            recorder.record_context_sources(rendered.sources)
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for("session-source-meta", "run-source-meta").read_text(
                    encoding="utf-8",
                )
            )

        ghost = payload["prompt_sections"][0]
        self.assertEqual(ghost["capability_id"], "local_context")
        self.assertEqual(ghost["admission_reason"], "run_start_assembly")
        plain = payload["prompt_sections"][1]
        self.assertNotIn("capability_id", plain)
        self.assertNotIn("admission_reason", plain)

    def test_tool_contract_hashes_are_stable_and_scope_aware(self) -> None:
        coding_full = model_tool_contract_hash()
        coding_readonly = model_tool_contract_hash(
            definitions_for_tool_names(("list_dir", "read_file"))
        )
        research_full = research_tool_contract_hash(include_source_search=True)
        research_thin = research_tool_contract_hash(include_source_search=False)
        controller_full = controller_action_contract_hash(include_source_search=True)
        controller_thin = controller_action_contract_hash(include_source_search=False)

        self.assertEqual(coding_full, model_tool_contract_hash())
        self.assertNotEqual(coding_full, coding_readonly)
        self.assertNotEqual(research_full, research_thin)
        self.assertNotEqual(controller_full, research_full)
        self.assertNotEqual(controller_full, controller_thin)
        self.assertTrue(coding_full.startswith("sha256:"))
        self.assertTrue(research_full.startswith("sha256:"))
        self.assertTrue(controller_full.startswith("sha256:"))

    def test_run_trace_records_model_and_runtime_tool_contract_hashes_separately(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id="run-contract-surfaces",
                session_id="session-contract-surfaces",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            model_hash = controller_action_contract_hash(include_source_search=True)
            runtime_hash = research_tool_contract_hash(include_source_search=True)

            recorder.record_tool_contract_hash(model_hash, phase="research")
            recorder.record_runtime_tool_contract_hash(runtime_hash, phase="research")
            recorder.finish(status="done")

            payload = json.loads(
                store.path_for(
                    "session-contract-surfaces",
                    "run-contract-surfaces",
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(payload["model_tool_contract_hash"], model_hash)
        self.assertEqual(payload["runtime_tool_contract_hash"], runtime_hash)
        self.assertIn({"hash": runtime_hash, "surface": "runtime", "phase": "research"}, payload["tool_contracts"])

    def test_agent_trace_recorder_preserves_prompt_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            (project / "app.py").write_text("print('hi')\n", encoding="utf-8")
            baseline_provider = _PromptProvider()
            traced_provider = _PromptProvider()

            agent.run(
                baseline_provider,
                project,
                "Inspect the project",
                max_turns=1,
                fresh_chat=False,
            )
            agent.run(
                traced_provider,
                project,
                "Inspect the project",
                max_turns=1,
                fresh_chat=False,
                trace_recorder=_NoopTrace(),
            )

            self.assertEqual(traced_provider.prompts, baseline_provider.prompts)

    def test_real_agent_run_stamps_epoch_metadata_on_outbound_sections(self) -> None:
        recorded: list[dict[str, object]] = []

        class _CapturingTrace:
            def record_prompt_section(self, name, text, **kwargs) -> None:
                del text
                recorded.append({"name": name, **kwargs})

            def __getattr__(self, _name):
                def call(*_args, **_kwargs):
                    return None
                return call

        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()

            agent.run(
                _PromptProvider(),
                project,
                "Inspect the project",
                max_turns=1,
                fresh_chat=False,
                trace_recorder=_CapturingTrace(),
            )

        outbound = [item for item in recorded if item["name"] == "coding_outbound_prompt"]
        self.assertTrue(outbound)
        first = outbound[0]
        self.assertEqual(first["freshness"], "provider_send")
        self.assertTrue(str(first["epoch_id"]).startswith("ctx_epoch:"))
        self.assertEqual(first["admission_reason"], "provider_turn_boundary")
        self.assertEqual(first["capability_id"], "agent_runner")


if __name__ == "__main__":
    unittest.main()


class AnalysisRunTraceTests(unittest.TestCase):
    def _open(self, store: RunTraceStore, *, run_id: str = "run-analysis") -> object:
        return store.open(
            run_id=run_id,
            session_id="session-analysis",
            project=None,
            mode_initial="project",
            provider_initial="deepseek",
        )

    def _payload(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_analysis_run_trace_is_bounded_deduplicated_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)

            recorder.record_analysis_run({
                "analysis_run_id": "analysis_run:" + "a" * 16,
                "run_id": "run-analysis",
                "tool_id": "1:0",
                "tool_name": "run",
                "command_digest": "sha256:" + "b" * 64,
                "command_display": "pytest -q",
                "cwd_ref": {"basename": "codey", "digest": "sha256:" + "c" * 64},
                "exit_code": 0,
                "ok": True,
                "started_at": "2026-08-22T08:00:00.000Z",
                "finished_at": "2026-08-22T08:00:01.500Z",
                "duration_ms": 1500,
                "managed_output_handle": "",
                "output_sha256": "",
                "stored_truncated": False,
                "capture_quality": "output_not_captured",
                "reproduction_status": "output_not_captured",
                "environment_digest": "sha256:" + "d" * 64,
                "warnings": ["timing_unavailable"],
                # Extra runtime-side fields must never reach the trace.
                "raw_stdout": "SECRET_STDOUT_SHOULD_NOT_BE_SAVED",
            })
            # Duplicate ref is ignored.
            recorder.record_analysis_run({
                "analysis_run_id": "analysis_run:" + "a" * 16,
                "command_display": "duplicate",
            })
            # Invalid ref shape is ignored.
            recorder.record_analysis_run({
                "analysis_run_id": "SECRET_ANALYSIS_RUN_REF",
                "command_display": "secret",
            })
            # Invalid tool instance id is ignored.
            recorder.record_analysis_run({
                "analysis_run_id": "analysis_run:" + "e" * 16,
                "run_id": "run-analysis",
                "tool_id": "run",
                "tool_name": "run",
                "command_digest": "sha256:" + "b" * 64,
                "command_display": "pytest -q",
            })
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-analysis", "run-analysis"))
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(len(payload["analysis_runs"]), 1)
            entry = payload["analysis_runs"][0]
            self.assertEqual(entry["analysis_run_id"], "analysis_run:" + "a" * 16)
            self.assertEqual(entry["tool_id"], "1:0")
            self.assertEqual(entry["tool_name"], "run")
            self.assertEqual(entry["command_digest"], "sha256:" + "b" * 64)
            self.assertEqual(entry["duration_ms"], 1500)
            self.assertEqual(entry["warnings"], ["timing_unavailable"])
            self.assertNotIn("raw_stdout", serialized)
            self.assertNotIn("SECRET", serialized)

    def test_analysis_run_trace_redacts_direct_command_display(self) -> None:
        secret_command = "pytest --api-key sk-abcdef1234567890abcdef --json"
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)

            recorder.record_analysis_run({
                "analysis_run_id": "analysis_run:" + "f" * 16,
                "run_id": "run-analysis",
                "tool_id": "1:0",
                "tool_name": "run",
                "command_digest": "sha256:" + "b" * 64,
                "command_display": secret_command,
                "ok": False,
                "capture_quality": "output_not_captured",
                "reproduction_status": "failed",
            })
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-analysis", "run-analysis"))
            serialized = json.dumps(payload, ensure_ascii=False)
            entry = payload["analysis_runs"][0]

            self.assertEqual(entry["command_display"], "")
            self.assertIn("command_display_redacted", entry["warnings"])
            self.assertNotIn(secret_command, serialized)
            self.assertNotIn("sk-abcdef", serialized)

    def test_analysis_runs_cap_at_max_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)

            for index in range(10):
                recorder.record_analysis_run({
                    "analysis_run_id": "analysis_run:" + f"{index:016x}",
                    "run_id": "run-analysis",
                    "tool_id": f"1:{index}",
                    "tool_name": "run",
                    "command_display": f"cmd {index}",
                    "command_digest": "sha256:" + f"{index:064x}",
                    "ok": True,
                    "capture_quality": "output_not_captured",
                    "reproduction_status": "output_not_captured",
                })
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-analysis", "run-analysis"))
            self.assertEqual(len(payload["analysis_runs"]), 8)
            self.assertEqual(payload["analysis_runs"][-1]["analysis_run_id"], "analysis_run:" + f"{9:016x}")
            self.assertIn("analysis_runs_truncated", payload["warnings"])

    def test_artifact_refs_dedupe_by_version_and_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)

            recorder.record_artifact_refs([{
                "artifact_id": "artifact:" + "1" * 16,
                "version_id": "artifact_version:" + "2" * 16,
                "artifact_kind": "managed_output",
                "sha256": "sha256:" + "3" * 64,
                "size": 40000,
                "mime": "text/plain",
                "origin_run_id": "run-analysis",
                "produced_by": "analysis_run:" + "4" * 16,
                "stored_truncated": False,
                "derived_from": [],
            }])
            # Same version id -> ignored.
            recorder.record_artifact_refs([{
                "artifact_id": "artifact:" + "1" * 16,
                "version_id": "artifact_version:" + "2" * 16,
                "mime": "text/plain",
            }])
            # Invalid version id -> ignored.
            recorder.record_artifact_refs([{"version_id": "NOT_A_REF"}])
            # Invalid artifact id -> ignored even when version id is valid.
            recorder.record_artifact_refs([{
                "artifact_id": "",
                "version_id": "artifact_version:" + "8" * 16,
                "mime": "text/plain",
            }])
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-analysis", "run-analysis"))
            self.assertEqual(len(payload["artifact_refs"]), 1)
            self.assertEqual(payload["artifact_refs"][0]["version_id"], "artifact_version:" + "2" * 16)

    def test_capsule_snapshot_replaces_same_capsule_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)
            capsule_id = "capsule:" + "5" * 16

            recorder.record_reproducibility_capsule({
                "capsule_id": capsule_id,
                "run_id": "run-analysis",
                "analysis_run_refs": ["analysis_run:" + "6" * 16],
                "reproduction_status": "output_not_captured",
            })
            recorder.record_reproducibility_capsule({
                "capsule_id": capsule_id,
                "run_id": "run-analysis",
                "analysis_run_refs": ["analysis_run:" + "6" * 16],
                "reproduction_status": "failed",
                "warnings": ["mixed_output_capture"],
            })
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-analysis", "run-analysis"))
            self.assertEqual(len(payload["reproducibility_capsules"]), 1)
            snapshot = payload["reproducibility_capsules"][0]
            self.assertEqual(snapshot["reproduction_status"], "failed")
            self.assertEqual(snapshot["warnings"], ["mixed_output_capture"])

    def test_capsule_artifact_refs_are_bounded_independently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)

            recorder.record_reproducibility_capsule({
                "capsule_id": "capsule:" + "9" * 16,
                "run_id": "run-analysis",
                "analysis_run_refs": ["analysis_run:" + "6" * 16],
                "artifact_refs": [
                    "artifact_version:" + f"{index:016x}"
                    for index in range(10)
                ],
                "reproduction_status": "output_captured",
            })
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-analysis", "run-analysis"))
            snapshot = payload["reproducibility_capsules"][0]
            self.assertEqual(len(snapshot["artifact_refs"]), 8)
            self.assertEqual(snapshot["artifact_refs"][-1], "artifact_version:" + f"{7:016x}")


class ReviewFindingTraceTests(unittest.TestCase):
    def _open(self, store: RunTraceStore, *, run_id: str = "run-findings") -> object:
        return store.open(
            run_id=run_id,
            session_id="session-findings",
            project=None,
            mode_initial="research",
            provider_initial="deepseek",
        )

    def _payload(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_review_findings_trace_is_ref_only_deduplicated_and_secret_free(self) -> None:
        from codey.research.review_finding import ReviewFindingRecord

        finding = ReviewFindingRecord(
            finding_id="review_finding:" + "a" * 16,
            kind="unsupported_claim",
            severity="critical",
            target_ref="claim:" + "b" * 16,
            claim_ref="claim:" + "b" * 16,
            evidence_ref="evidence:" + "c" * 16,
            proof_ref="research_proof:" + "d" * 16,
            reason_codes=("claim_missing_support_relation",),
        )
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)

            recorder.record_review_findings([finding])
            # Duplicate id is ignored; malformed entries are ignored.
            recorder.record_review_findings([
                ReviewFindingRecord(
                    finding_id="review_finding:" + "a" * 16,
                    kind="unsupported_claim",
                    severity="critical",
                ),
                {"finding_id": "not-a-ref", "kind": "unsupported_claim"},
                {
                    "finding_id": "review_finding:" + "e" * 16,
                    "kind": "made_up_finding",
                    "severity": "critical",
                    "message": "RAW MESSAGE SHOULD_NOT_BE_SAVED",
                },
                "junk",
                None,
            ])
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-findings", "run-findings"))
            serialized = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(len(payload["research_review_findings"]), 1)
            entry = payload["research_review_findings"][0]
            self.assertEqual(entry["finding_id"], "review_finding:" + "a" * 16)
            self.assertEqual(entry["kind"], "unsupported_claim")
            self.assertEqual(entry["severity"], "critical")
            self.assertEqual(entry["status"], "open")
            self.assertEqual(entry["target_ref"], "claim:" + "b" * 16)
            self.assertEqual(entry["reason_codes"], ["claim_missing_support_relation"])
            self.assertNotIn("message", entry)
            self.assertNotIn("made_up_finding", serialized)
            self.assertNotIn("SHOULD_NOT_BE_SAVED", serialized)

    def test_review_findings_direct_recorder_normalizes_invalid_severity_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)

            recorder.record_review_findings([{
                "finding_id": "review_finding:" + "f" * 16,
                "kind": "unsupported_claim",
                "severity": "urgent",
                "status": "model_fixed",
                "target_ref": "claim:" + "b" * 16,
                "message": "RAW MESSAGE SHOULD_NOT_BE_SAVED",
            }])
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-findings", "run-findings"))
            serialized = json.dumps(payload, ensure_ascii=False)
            finding = payload["research_review_findings"][0]

            self.assertEqual(finding["severity"], "warning")
            self.assertEqual(finding["status"], "open")
            self.assertNotIn("urgent", serialized)
            self.assertNotIn("model_fixed", serialized)
            self.assertNotIn("message", finding)
            self.assertNotIn("SHOULD_NOT_BE_SAVED", serialized)

    def test_review_findings_cap_at_max_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)

            for index in range(20):
                recorder.record_review_findings([{
                    "finding_id": "review_finding:" + f"{index:016x}",
                    "kind": "stale_source",
                    "severity": "warning",
                    "status": "open",
                    "target_ref": "source:" + f"{index:016x}",
                    "reason_codes": ["sources_stale_or_undated"],
                }])
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-findings", "run-findings"))
            findings = payload["research_review_findings"]
            self.assertEqual(len(findings), 16)
            self.assertEqual(findings[-1]["finding_id"], "review_finding:" + f"{19:016x}")
            self.assertIn("research_review_findings_truncated", payload["warnings"])

    def test_planner_gaps_trace_keeps_valid_refs_and_drops_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store)

            recorder.record_planner_gaps([{
                "gap_id": "planner_gap:" + "e" * 16,
                "gap_kind": "followup_search",
                "target_ref": "claim:" + "b" * 16,
                "reason_codes": ["claim_missing_support_relation"],
                "finding_refs": [
                    "review_finding:" + "a" * 16,
                    "https://evil.example/not-a-ref",
                    "junk",
                ],
            }])
            recorder.record_planner_gaps([{
                "gap_id": "planner_gap:short",
                "gap_kind": "locator_verification",
            }])
            recorder.record_planner_gaps([{
                "gap_id": "planner_gap:" + "f" * 16,
                "gap_kind": "made_up_gap",
                "target_ref": "claim:" + "b" * 16,
            }])
            # Duplicate is ignored.
            recorder.record_planner_gaps([{
                "gap_id": "planner_gap:" + "e" * 16,
                "gap_kind": "rerun_analysis",
            }])
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-findings", "run-findings"))
            serialized = json.dumps(payload, ensure_ascii=False)
            gaps = payload["research_planner_gaps"]

            self.assertEqual(len(gaps), 1)
            self.assertNotIn("made_up_gap", serialized)
            gap = gaps[0]
            self.assertEqual(gap["gap_id"], "planner_gap:" + "e" * 16)
            self.assertEqual(gap["gap_kind"], "followup_search")
            self.assertEqual(gap["target_ref"], "claim:" + "b" * 16)
            self.assertEqual(gap["finding_refs"], ["review_finding:" + "a" * 16])

    def test_manifest_without_findings_keeps_empty_sections_and_old_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(td)
            recorder = self._open(store, run_id="run-plain")
            recorder.finish(status="done")

            payload = self._payload(store.path_for("session-findings", "run-plain"))

            self.assertEqual(payload["research_review_findings"], [])
            self.assertEqual(payload["research_planner_gaps"], [])
            self.assertNotIn("research_review_findings_truncated", payload["warnings"])
