from __future__ import annotations

from tests.manual import scoped_task_plan_ab


def test_extract_paths_and_tests_from_json_with_prose() -> None:
    text = 'ignore this\n{"paths":["./codey/app/task_runner.py"],"test_paths":["tests/test_server.py"]}'

    assert scoped_task_plan_ab._paths_from_reply(text) == ("codey/app/task_runner.py",)
    assert scoped_task_plan_ab._test_paths_from_reply(text) == ("tests/test_server.py",)


def test_path_score_accepts_suffix_paths() -> None:
    score = scoped_task_plan_ab._score_paths(
        ("task_runner.py",),
        ("codey/app/task_runner.py",),
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
    assert "codey/agents/writer_failover.py" in prompt


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


def test_summary_without_current_omits_deltas() -> None:
    rows = [
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

    summary = scoped_task_plan_ab._summarize_rows(rows, arms=("scoped",))

    assert summary["by_arm"]["scoped"]["ok"] == 1
    assert summary["deltas_vs_current"] == {}


def test_main_allows_single_scoped_arm(tmp_path, monkeypatch) -> None:
    case = scoped_task_plan_ab.ProbeCase(
        name="single-scoped",
        project=tmp_path,
        task="Pick files.",
        expected_paths=("codey/app/task_runner.py",),
    )
    row = {
        "case": case.name,
        "arm": "scoped",
        "ok": True,
        "score": {
            "paths": {"hit_count": 1, "top1_hit": True},
            "tests": {"hit_count": 0},
            "terms": {"hit_count": 0},
        },
        "sent_chars": 100,
        "provider_seconds": 1.0,
    }
    called: dict[str, tuple[str, ...]] = {}

    def fake_run_provider(provider_id, selected_cases, *, port, timeout, order, arms):
        called["arms"] = arms
        assert provider_id == "deepseek"
        assert selected_cases == (case,)
        return {
            "provider": provider_id,
            "rows": [row],
            "summary": scoped_task_plan_ab._summarize_rows([row], arms=arms),
        }

    monkeypatch.setattr(scoped_task_plan_ab, "provider_ids", lambda: ("deepseek",))
    monkeypatch.setattr(scoped_task_plan_ab, "cases", lambda stockalarm=None: {case.name: case})
    monkeypatch.setattr(scoped_task_plan_ab, "run_provider", fake_run_provider)

    output = tmp_path / "result.json"

    assert (
        scoped_task_plan_ab.main(
            [
                "--provider",
                "deepseek",
                "--case",
                case.name,
                "--arms",
                "scoped",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert called["arms"] == ("scoped",)
    assert output.exists()
