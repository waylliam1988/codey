from tests.manual import coding_current_context_ab as probe


def test_coding_current_context_ab_self_test() -> None:
    probe._self_test()


def test_run_arm_uses_production_context_toggle() -> None:
    case = probe.ContextCase(
        name="read-only-test",
        task="Read app.py.",
        files={"app.py": "VALUE = 1\n"},
        expected_changed=(),
        check_command=(probe.sys.executable, "-c", "pass"),
    )
    baseline = probe._run_arm(
        probe._ScriptedProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"read app.py"}}',
        ),
        case,
        "baseline",
        max_turns=4,
    )
    context = probe._run_arm(
        probe._ScriptedProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"read app.py"}}',
        ),
        case,
        "context",
        max_turns=4,
    )

    assert baseline["context_prompt_count"] == 0
    assert context["context_prompt_count"] == 1


def test_summary_reports_context_delta() -> None:
    rows = [
        {
            "arm": "baseline",
            "success": True,
            "selected_check_passed_after_edit": False,
            "default_verification_reminders": 1,
            "duplicate_reads": 1,
            "protocol_errors": 0,
            "turns": 5,
            "tool_calls": 4,
            "sent_chars": 100,
        },
        {
            "arm": "context",
            "success": True,
            "selected_check_passed_after_edit": True,
            "default_verification_reminders": 0,
            "duplicate_reads": 0,
            "protocol_errors": 0,
            "turns": 4,
            "tool_calls": 3,
            "sent_chars": 130,
        },
    ]

    summary = probe._summarize(rows)

    assert summary["arms"]["context"]["selected_check_passed_after_edit"] == 1
    assert summary["context_delta_vs_baseline"]["selected_check_passed_after_edit"] == 1
    assert summary["context_delta_vs_baseline"]["default_verification_reminders"] == -1
    assert summary["context_delta_vs_baseline"]["duplicate_reads"] == -1


def test_changed_files_ignores_test_cache_artifacts() -> None:
    with probe.tempfile.TemporaryDirectory() as td:
        root = probe.Path(td)
        (root / "app.py").write_text("before\n", encoding="utf-8")
        cache = root / ".pytest_cache" / "v" / "cache"
        cache.mkdir(parents=True)
        (cache / "nodeids").write_text("noise\n", encoding="utf-8")
        (root / "app.py").write_text("after\n", encoding="utf-8")

        changed = probe._changed_files(root, {"app.py": "before\n"})

    assert changed == ("app.py",)
