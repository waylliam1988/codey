from __future__ import annotations

import tempfile
from pathlib import Path

from tests.manual import task_lens_ab


def test_task_lens_replaces_task_navigation_without_stacking() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cases = task_lens_ab.build_deep_fixture(root)
        case = cases["billing-proration"]

        current = task_lens_ab.render_current_project_map(root, case.task)
        lens = task_lens_ab.render_lens_project_map(root, case.task)

    assert "Focused subtree" in current
    assert "Task Lens" in lens
    assert "Focused subtree" not in lens
    assert "Symbol overview" not in lens
    assert case.expected_paths[0] in lens
    assert case.expected_tests[0] in lens


def test_task_lens_is_compact_and_reports_omissions_without_examples() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cases = task_lens_ab.build_deep_fixture(root)
        case = cases["billing-proration"]
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "binary_router.py").write_bytes(b"\xff\xfe\x00")
        (root / "src" / "big_router.py").write_text(
            "def big_router_dispatch():\n    return True\n"
            + ("#" * (task_lens_ab.MAX_FOCUS_SOURCE_BYTES + 1)),
            encoding="utf-8",
        )

        lens = task_lens_ab.build_task_lens(root, case.task)

    assert len(lens) <= task_lens_ab.TASK_LENS_HARD_CHARS
    assert "omitted[kind,count,reason]" in lens
    assert "oversized" in lens
    assert "non-UTF-8" in lens
    assert "binary_router.py" not in lens
    assert "big_router.py" not in lens


def test_task_lens_does_not_leak_secret_or_symlink_paths() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src").mkdir()
        (root / "src" / "visible_router.py").write_text(
            "def visible_router_dispatch():\n    return True\n",
            encoding="utf-8",
        )
        (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "router.js").write_text(
            "export function visibleRouterDispatch() { return true }\n",
            encoding="utf-8",
        )
        link = root / "src" / "linked_router.py"
        try:
            link.symlink_to(root / "src" / "visible_router.py")
        except OSError:
            link = None

        lens = task_lens_ab.build_task_lens(root, "visible router dispatch")

    assert "src/visible_router.py" in lens
    assert ".env" not in lens
    assert "SECRET=1" not in lens
    assert "node_modules" not in lens
    if link is not None:
        assert "linked_router.py" not in lens


def test_task_lens_selection_scoring_tracks_top3() -> None:
    score = task_lens_ab._score_paths(
        ("wrong.py", "also_wrong.py", "proration_policy.py"),
        ("apps/commerce/src/domain/billing/policies/proration_policy.py",),
    )

    assert score["hit_count"] == 1
    assert not score["top1_hit"]
    assert score["top3_hit"]


def test_summary_reports_lens_prompt_delta() -> None:
    rows = [
        {
            "mode": "pick",
            "arm": "current",
            "target_named_in_task": False,
            "tags": ["deep"],
            "ok": True,
            "score": {
                "paths": {"hit_count": 1, "top1_hit": False, "top3_hit": True},
                "tests": {"hit_count": 0},
            },
            "sent_chars": 200,
            "prompt_chars": 200,
            "provider_seconds": 1.0,
        },
        {
            "mode": "pick",
            "arm": "lens",
            "target_named_in_task": False,
            "tags": ["deep"],
            "ok": True,
            "score": {
                "paths": {"hit_count": 2, "top1_hit": True, "top3_hit": True},
                "tests": {"hit_count": 1},
            },
            "sent_chars": 180,
            "prompt_chars": 180,
            "provider_seconds": 1.2,
        },
    ]

    summary = task_lens_ab._summary_with_subsets(rows)
    delta = summary["unnamed_deep"]["deltas_vs_current"]["lens"]

    assert delta["path_hits"] == 1
    assert delta["test_hits"] == 1
    assert delta["top1_path_hits"] == 1
    assert delta["prompt_chars"] == -20
