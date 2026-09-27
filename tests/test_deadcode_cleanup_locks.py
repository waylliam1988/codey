"""Red-first locks for dead-code/consistency cleanup (5 items).

These tests assert the POST-cleanup state. They must FAIL before the fix
and PASS after. Each failure is deterministic (no network/browser).
"""
from __future__ import annotations

import inspect
from pathlib import Path


def _review() -> dict:
    return {
        "proof_ref": "research_proof:" + "a" * 16,
        "question_digest": "sha256:" + "b" * 64,
        "ok": False,
        "answers_question": False,
        "answer_status": "partial",
        "coverage_gaps": [{"reason_code": "x", "term_ref": "hepatotoxicity"}],
        "followup_questions": [{"text": "Find clinical evidence gap"}],
        "query_rewrite_candidates": [],
        "missing_evidence": ["answer_coverage_gap"],
    }


def test_registry_only_lists_executable_connectors() -> None:
    from codey.research.source_connectors import built_in_connector_registry

    registry = built_in_connector_registry()
    assert registry.ids() == ("arxiv", "pubmed")
    assert registry.shipped_fixture_ids() == ("arxiv", "pubmed")


def test_local_csv_json_questions_never_plan_unexecutable_connectors() -> None:
    from codey.research.query_planner import build_research_plan

    for question in (
        "Use the local CSV table dataset to compare rows 本地文件 数据",
        "Use the local JSON file data",
        "打开本地文件 CSV 数据 JSON 调查",
    ):
        plan = build_research_plan(
            _review(),
            question=question,
        )
        ids = [item.connector_id for item in plan.source_preferences]
        assert "local_file" not in ids, question
        assert "csv_tsv" not in ids, question
        assert "json_file" not in ids, question
        assert "openalex" not in ids, question
        assert "rss" not in ids, question
        for cand in plan.query_candidates:
            assert "local_file" not in cand.connector_ids
            assert "csv_tsv" not in cand.connector_ids
            assert "json_file" not in cand.connector_ids
        # suffixes must not claim local/table/structured evidence
        for cand in plan.query_candidates:
            low = cand.query_preview.casefold()
            assert "table evidence" not in low, cand.query_preview
            assert "structured data" not in low, cand.query_preview
            assert "local source" not in low, cand.query_preview


def test_planner_has_no_openalex_rss_warnings() -> None:
    from codey.research.query_planner import build_research_plan

    plan = build_research_plan(_review(), question="clinical disease therapy")
    assert "openalex_deferred" not in plan.warnings
    assert "rss_optional" not in plan.warnings


def test_local_fetch_helpers_are_removed() -> None:
    import codey.research.source_connectors as sc

    for name in (
        "fetch_local_file",
        "fetch_csv_tsv_file",
        "fetch_json_file",
        "resolve_local_source_path",
    ):
        assert not hasattr(sc, name), name
    assert "fetch_local_file" not in sc.__all__
    assert "fetch_csv_tsv_file" not in sc.__all__
    assert "fetch_json_file" not in sc.__all__
    assert "resolve_local_source_path" not in sc.__all__


def test_connector_terms_only_route_pubmed_arxiv() -> None:
    from codey.research import connector_terms as ct
    from codey.research.connector_terms import preferred_connector_ids

    assert not hasattr(ct, "LOCAL_CONNECTOR_TERMS")
    # even when local terms appear, no local connector is suggested
    out = preferred_connector_ids(
        ("csv", "json", "local", "table", "dataset", "file"),
        available_ids=("arxiv", "pubmed", "local_file", "csv_tsv", "json_file"),
    )
    assert "local_file" not in out
    assert "csv_tsv" not in out
    assert "json_file" not in out
    # normal routing still works
    assert "pubmed" in preferred_connector_ids(("clinical", "cancer"), available_ids=("arxiv", "pubmed"))
    assert "arxiv" in preferred_connector_ids(("transformer",), available_ids=("arxiv", "pubmed"))


def test_secret_boundary_still_keeps_data_words_after_marker() -> None:
    # Deterministic redaction stability: removing local connectors must not
    # turn ordinary data words into secret values. "csv/json/table/…" after
    # a secret marker must survive as query terms (boundary), only the
    # marker itself is redacted.
    from codey.research.source_connectors import safe_connector_query

    for word in ("csv", "json", "table", "dataset", "local", "file"):
        safe = safe_connector_query(f"api key {word} clinical cancer")
        assert word in safe.terms, word
        assert "clinical" in safe.terms
        assert "cancer" in safe.terms


def test_domain_profile_system_is_removed() -> None:
    import codey.research.query_planner as qp

    root = Path(__file__).resolve().parents[1]
    assert not (root / "codey" / "research" / "domain_profiles.py").exists()
    try:
        import codey.research.domain_profiles  # noqa: F401
    except ImportError:
        pass
    else:
        raise AssertionError("domain_profiles module should be removed")
    sig = inspect.signature(qp.build_research_plan)
    assert "evidence_profile" not in sig.parameters
    sig2 = inspect.signature(qp._source_preferences)
    assert "evidence_profile" not in sig2.parameters
    assert not hasattr(qp, "_PROFILE_CONNECTOR_KINDS")
    assert not hasattr(qp, "_profile_connector_kinds")


def test_finding_lifecycle_is_removed_audit_projection_kept() -> None:
    import codey.research.review_finding as rf

    assert not hasattr(rf, "apply_finding_events")
    assert not hasattr(rf, "ReviewFindingEvent")
    assert not hasattr(rf, "failed_analysis_findings")
    assert not hasattr(rf, "EVENT_ADDRESSED")
    assert not hasattr(rf, "EVENT_CONFIRMED")
    assert not hasattr(rf, "EVENT_REJECTED")
    assert not hasattr(rf, "EVENT_ACTIONS")
    assert not hasattr(rf, "CONFIRMATION_SOURCES")
    assert not hasattr(rf, "_append_reason")
    # audit core stays
    assert hasattr(rf, "findings_from_proof_review")
    assert hasattr(rf, "planner_gaps_from_findings")
    assert hasattr(rf, "ReviewFindingRecord")
    assert hasattr(rf, "PlannerGap")
    assert "apply_finding_events" not in rf.__all__
    assert "ReviewFindingEvent" not in rf.__all__
    assert "failed_analysis_findings" not in rf.__all__


def test_browser_openers_are_unified() -> None:
    from codey.automation import browser
    from codey.providers import web_provider as wp

    for name in ("open_deepseek", "open_qwen", "open_mimo", "open_stepfun", "open_glm"):
        assert not hasattr(browser, name), name
    assert hasattr(browser, "open_chat_page")
    # provider maps cover all five ids
    assert set(browser.PROVIDER_START_URLS) == {"deepseek", "qwen", "mimo", "stepfun", "glm"}
    assert set(browser.PROVIDER_URL_CONTAINS) == {"deepseek", "qwen", "mimo", "stepfun", "glm"}
    # spec no longer carries opener indirection
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(wp.WebProviderSpec)}
    assert "opener_name" not in field_names
    # connect() must resolve via provider_id + shared maps (deterministic, no browser)
    from unittest import mock

    for provider_cls, pid in (
        (wp.DeepSeekWebProvider, "deepseek"),
        (wp.QwenWebProvider, "qwen"),
        (wp.MimoWebProvider, "mimo"),
        (wp.StepFunWebProvider, "stepfun"),
        (wp.GlmWebProvider, "glm"),
    ):
        assert provider_cls.spec.provider_id == pid
        sentinel = object()
        with mock.patch.object(browser, "open_chat_page", return_value=sentinel) as opened:
            prov = provider_cls.connect(port=9333)
            assert prov.session is sentinel
            opened.assert_called_once()
            _, kwargs = opened.call_args
            # positional start_url/url_contains come from shared maps
            args = opened.call_args.args
            assert args[0] == browser.PROVIDER_START_URLS[pid]
            assert args[1] == browser.PROVIDER_URL_CONTAINS[pid]
            assert kwargs["port"] == 9333


def test_mode_selection_trace_replaces_router() -> None:
    from codey.runs import trace as tr

    assert hasattr(tr, "ModeSelectionTrace")
    assert not hasattr(tr, "RouterTrace")
    assert hasattr(tr.RunTraceRecorder, "record_mode_selection")
    assert not hasattr(tr.RunTraceRecorder, "record_router")
    assert "mode_selection" in tr.RunTraceManifest.__dataclass_fields__
    assert "router" not in tr.RunTraceManifest.__dataclass_fields__
    # payload uses new key only
    m = tr.RunTraceManifest(run_id="r1", session_id="s1")
    payload = m.to_payload()
    assert "mode_selection" in payload
    assert "router" not in payload
    # call sites use new name
    root = Path(__file__).resolve().parents[1]
    dispatch_src = (root / "codey" / "operations" / "task_phases" / "dispatch.py").read_text(encoding="utf-8")
    task_run_src = (root / "codey" / "operations" / "task_run.py").read_text(encoding="utf-8")
    assert "record_mode_selection" in dispatch_src
    assert "record_mode_selection" in task_run_src
    assert "record_router" not in dispatch_src
    assert '"record_router"' not in task_run_src and "'record_router'" not in task_run_src


# --- Phase 2: dead-param / dedup / hierarchy locks (must FAIL before fix) ---

def test_evaluate_tool_call_policy_has_no_dead_turn_params() -> None:
    import inspect

    from codey.agents import tool_execution as te

    params = inspect.signature(te.evaluate_tool_call_policy).parameters
    assert "turn" not in params
    assert "tool_index" not in params
    # intent builder still owns turn/tool_index (outer loop keeps them)
    intent_params = inspect.signature(te.build_tool_call_intent).parameters
    assert "turn" in intent_params and "tool_index" in intent_params


def test_run_headless_task_has_no_emit_jsonl() -> None:
    import inspect

    from codey.app import headless_runner as hr

    params = inspect.signature(hr._run_headless_task).parameters
    assert "emit_jsonl" not in params
    # outer entry still needs it
    assert "emit_jsonl" in inspect.signature(hr.run_headless).parameters


def test_connect_and_build_frame_has_no_work() -> None:
    import inspect

    from codey.operations.task_phases import dispatch as dp

    assert "work" not in inspect.signature(dp.connect_and_build_frame).parameters


def test_route_ghost_work_has_no_deps() -> None:
    import inspect

    from codey.operations import task_run as tr

    assert "deps" not in inspect.signature(tr._route_ghost_work).parameters


def test_run_hybrid_mode_has_no_deps() -> None:
    import inspect

    from codey.operations import research_flow as rf

    assert "deps" not in inspect.signature(rf.run_hybrid_mode).parameters


def test_resolve_path_has_no_root() -> None:
    import inspect

    from codey.policies import run_command_semantics as rcs

    assert "root" not in inspect.signature(rcs._resolve_path).parameters
    # boundary check stays in the caller
    src = inspect.getsource(rcs._resolve_inside_project)
    assert "candidate != root" in src or "candidate.parents" in src


def test_research_note_payload_has_no_ctx() -> None:
    import inspect

    from codey.app import api as api_mod

    assert "ctx" not in inspect.signature(api_mod._research_note_payload).parameters


def test_require_supersedable_has_no_delivery_batch() -> None:
    import inspect

    from codey.runtime.write import provider_effects as pe

    params = inspect.signature(pe._require_supersedable_not_sent).parameters
    assert "delivery_batch_id" not in params
    # outer builder still needs the batch id
    assert "delivery_batch_id" in inspect.signature(pe.build_provider_begin_rows).parameters


def test_review_relation_rows_has_no_assumptions() -> None:
    import inspect

    from codey.research import proof_quality as pq

    params = inspect.signature(pq._review_relation_rows).parameters
    assert "assumptions" not in params
    assert "assumption_ids" in params


def test_provider_replay_policy_has_no_purpose() -> None:
    import inspect

    from codey.runtime.effects import replay_policy as rp

    assert "purpose" not in inspect.signature(rp.provider_replay_policy).parameters
    # always unsafe regardless of input
    assert rp.provider_replay_policy().reason == "outbound_provider_call"


def test_render_results_has_no_final_url() -> None:
    import inspect

    from codey.research import source_search as ss

    assert "final_url" not in inspect.signature(ss.render_results).parameters
    assert "hits" in inspect.signature(ss.render_results).parameters


def test_stepfun_submission_chain_has_no_dead_text_params() -> None:
    import inspect

    from codey.providers.web_drivers import stepfun as sf

    assert "submitted_text" not in inspect.signature(sf._submission_started).parameters
    assert "submitted_text" not in inspect.signature(sf._wait_submission_started).parameters
    submit_params = inspect.signature(sf._submit).parameters
    assert "submitted_text" not in submit_params
    assert "textarea" not in submit_params
    # filling still checks text stability (covered executably by test_stepfun
    # refill/reject tests; no tautology here)
    assert "submitted_text" in inspect.signature(sf._composer_retains_text).parameters


def test_provider_ids_single_source_is_catalog() -> None:
    from codey.providers import catalog as cat
    from codey.providers import registry as reg

    assert reg.provider_ids is cat.provider_ids
    src = inspect.getsource(reg)
    assert "def provider_ids" not in src
    assert "from codey.providers.catalog import" in src and "provider_ids" in src


def test_clean_sha256_single_shared_helper() -> None:
    from codey.utils import refs as refs_mod

    assert hasattr(refs_mod, "clean_sha256_hex")
    assert refs_mod.clean_sha256_hex("A" * 64) == "a" * 64
    assert refs_mod.clean_sha256_hex("sha256:" + "a" * 64) == ""
    assert refs_mod.clean_sha256_hex("xyz") == ""
    import codey.research.analysis_run as ar
    import codey.research.artifact_lineage as al

    assert ar.clean_sha256_hex is refs_mod.clean_sha256_hex or getattr(ar, "_clean_sha256", None) is refs_mod.clean_sha256_hex
    assert al.clean_sha256_hex is refs_mod.clean_sha256_hex or getattr(al, "_clean_sha256", None) is refs_mod.clean_sha256_hex
    assert "_SHA256_RE" not in dir(ar) or "clean_sha256_hex" in dir(ar)
    root = Path(__file__).resolve().parents[1]
    ar_src = (root / "codey" / "research" / "analysis_run.py").read_text(encoding="utf-8")
    al_src = (root / "codey" / "research" / "artifact_lineage.py").read_text(encoding="utf-8")
    assert "_SHA256_RE" not in ar_src
    assert "_SHA256_RE" not in al_src
    assert "clean_sha256_hex" in ar_src
    assert "clean_sha256_hex" in al_src


def test_atomic_write_has_no_local_wrappers() -> None:
    root = Path(__file__).resolve().parents[1]
    store_src = (root / "codey" / "knowledge" / "store.py").read_text(encoding="utf-8")
    managed_src = (root / "codey" / "storage" / "managed_outputs.py").read_text(encoding="utf-8")
    assert "def _atomic_write_text" not in store_src
    assert "def _write_text_atomic" not in managed_src
    assert "from codey.storage.atomic_io import write_text_atomic" in store_src
    assert "from codey.storage.atomic_io import write_text_atomic" in managed_src
    assert "write_text_atomic(path" in store_src
    assert "write_text_atomic(path" in managed_src


def test_web_provider_has_no_intermediate_base() -> None:
    import inspect

    from codey.providers import web_provider as wp

    assert not hasattr(wp, "_provider_class")
    root = Path(__file__).resolve().parents[1]
    src = (root / "codey" / "providers" / "web_provider.py").read_text(encoding="utf-8")
    assert "def _provider_class" not in src
    assert "_provider_class(" not in src
    for cls in (
        wp.DeepSeekWebProvider,
        wp.MimoWebProvider,
        wp.StepFunWebProvider,
        wp.QwenWebProvider,
        wp.GlmWebProvider,
    ):
        assert inspect.getmro(cls)[1] is wp.WebChatProvider
        assert isinstance(cls.spec, wp.WebProviderSpec)
    assert set(wp.WEB_PROVIDER_CLASSES) == {"deepseek", "mimo", "stepfun", "qwen", "glm"}
