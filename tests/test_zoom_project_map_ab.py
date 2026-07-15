from __future__ import annotations

import tempfile
from pathlib import Path

from tests.manual import zoom_project_map_ab


def test_zoom_fixture_hides_deep_target_from_current_map() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cases = zoom_project_map_ab.build_deep_fixture(root)
        case = cases["billing-proration"]

        current = zoom_project_map_ab.render_legacy_project_map(root, case.task)
        zoom = zoom_project_map_ab.render_zoom_project_map(root, case.task)

    assert case.expected_paths[0] not in current
    assert case.expected_paths[0] in zoom
    assert "Focused subtree" in zoom
    assert "- apps/commerce/" in zoom


def test_zoom_prompt_marks_probe_arm_and_includes_exact_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cases = zoom_project_map_ab.build_deep_fixture(root)
        case = cases["digest-quiet-window"]

        prompt = zoom_project_map_ab._selection_prompt(case, root, arm="zoom")

    assert "Map arm: zoom" in prompt
    assert "Focused subtree" in prompt
    assert "services/messaging/src/pipelines/digest/quiet_hours.py" in prompt


def test_path_score_accepts_basename_and_suffix() -> None:
    score = zoom_project_map_ab._score_paths(
        ("proration_policy.py",),
        ("apps/commerce/src/domain/billing/policies/proration_policy.py",),
    )

    assert score["hit_count"] == 1
    assert score["top1_hit"]


def test_extract_paths_and_test_paths_from_json_with_prose() -> None:
    reply = (
        'prose\n{"paths":["./apps/commerce/src/domain/billing/policies/'
        'proration_policy.py"],"test_paths":["apps/commerce/tests/billing/'
        'test_proration_policy.py"]}'
    )

    assert zoom_project_map_ab._paths_from_reply(reply) == (
        "apps/commerce/src/domain/billing/policies/proration_policy.py",
    )
    assert zoom_project_map_ab._test_paths_from_reply(reply) == (
        "apps/commerce/tests/billing/test_proration_policy.py",
    )


def test_summary_reports_unnamed_deep_delta() -> None:
    rows = [
        {
            "arm": "current",
            "target_named_in_task": False,
            "tags": ["deep", "unnamed-target"],
            "ok": False,
            "score": {
                "paths": {"hit_count": 0, "top1_hit": False},
                "tests": {"hit_count": 0},
            },
            "sent_chars": 200,
            "prompt_chars": 200,
            "provider_seconds": 1.0,
        },
        {
            "arm": "zoom",
            "target_named_in_task": False,
            "tags": ["deep", "unnamed-target"],
            "ok": True,
            "score": {
                "paths": {"hit_count": 2, "top1_hit": True},
                "tests": {"hit_count": 1},
            },
            "sent_chars": 180,
            "prompt_chars": 180,
            "provider_seconds": 1.2,
        },
    ]

    summary = zoom_project_map_ab._summary_with_subsets(rows)
    delta = summary["unnamed_deep"]["deltas_vs_current"]["zoom"]

    assert delta["path_hits"] == 2
    assert delta["test_hits"] == 1
    assert delta["top1_path_hits"] == 1
    assert delta["sent_chars"] == -20
