from __future__ import annotations

from tests.manual import scoped_task_plan_ab


def test_extract_paths_and_tests_from_json_with_prose() -> None:
    text = (
        'ignore this\n{"paths":["./codey/task_runner.py"],'
        '"test_paths":["tests/test_server.py"]}'
    )

    assert scoped_task_plan_ab._paths_from_reply(text) == ("codey/task_runner.py",)
    assert scoped_task_plan_ab._test_paths_from_reply(text) == ("tests/test_server.py",)


def test_path_score_accepts_suffix_paths() -> None:
    score = scoped_task_plan_ab._score_paths(
        ("task_runner.py",),
        ("codey/task_runner.py",),
    )

    assert score["hit_count"] == 1
    assert score["top1_hit"]


def test_scoped_plan_prompt_is_private_and_non_persistent() -> None:
    case = scoped_task_plan_ab.cases()["scoped-navigation-brief"]

    prompt = scoped_task_plan_ab._scoped_plan_prompt(case)

    assert "Scoped Task Plan" in prompt
    assert "not persisted" in prompt
    assert "Do not invent persistence" in prompt
    assert "Project Map" in prompt


def test_deterministic_hint_is_local_and_advisory() -> None:
    case = scoped_task_plan_ab.cases()["writer-takeover-checkpoint"]

    prompt = scoped_task_plan_ab._hint_prompt(case)

    assert "Deterministic Scope Hint" in prompt
    assert "local and advisory" in prompt
    assert "Project Map" in prompt
    assert "codey/writer_failover.py" in prompt


def test_summary_reports_scoped_delta() -> None:
    rows = [
        {
            "arm": "current",
            "ok": True,
            "score": {
                "paths": {"hit_count": 1, "top1_hit": False},
                "tests": {"hit_count": 0},
                "terms": {"hit_count": 1},
            },
            "sent_chars": 100,
            "provider_seconds": 1.5,
        },
        {
            "arm": "scoped",
            "ok": True,
            "score": {
                "paths": {"hit_count": 2, "top1_hit": True},
                "tests": {"hit_count": 1},
                "terms": {"hit_count": 2},
            },
            "sent_chars": 220,
            "provider_seconds": 3.0,
        },
    ]

    summary = scoped_task_plan_ab._summarize_rows(rows)

    delta = summary["deltas_vs_current"]["scoped"]

    assert delta["path_hits"] == 1
    assert delta["test_hits"] == 1
    assert delta["top1_path_hits"] == 1
    assert delta["sent_chars"] == 120
