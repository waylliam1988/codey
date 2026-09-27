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
