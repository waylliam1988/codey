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


if __name__ == "__main__":
    unittest.main()
