from tests.manual import coding_repair_prompt_ab as probe


def test_coding_repair_prompt_ab_self_test() -> None:
    probe._self_test()


def test_typed_prompt_contains_specific_fix_not_generic_only() -> None:
    case = next(item for item in probe.CASES if item.name == "unknown_write_file")

    baseline = probe._prompt(case, "baseline")
    typed = probe._prompt(case, "typed")
    production_repair = probe._typed_repair(case)

    assert "unknown tool: write_file" in baseline
    assert "Coding has no write_file tool" in typed
    assert production_repair in typed
    assert "Preserve the previous intended path" in typed
    assert "Example preserving your previous intent" in typed
    assert '{"tool":"edit","args":{"path":"notes.txt","content":"hello\\n"}}' in typed
    assert "Your previous reply did not contain a valid JSON tool call" in baseline


def test_scoring_requires_expected_action_and_strict_json() -> None:
    case = next(item for item in probe.CASES if item.name == "native_tool_denial")

    good = probe._analyze_reply(case, '{"tool":"read_file","args":{"path":"app.py"}}')
    wrong = probe._analyze_reply(case, '{"tool":"done","args":{"summary":"cannot inspect"}}')
    noisy = probe._analyze_reply(
        case,
        'Here is the JSON:\n{"tool":"read_file","args":{"path":"app.py"}}',
    )

    assert good["accepted"] is True
    assert good["expected_action"] is True
    assert good["clean_repair"] is True
    assert wrong["accepted"] is True
    assert wrong["expected_action"] is False
    assert wrong["clean_repair"] is False
    assert noisy["accepted"] is True
    assert noisy["expected_action"] is True
    assert noisy["strict_single_json"] is False
    assert noisy["clean_repair"] is False


def test_summary_reports_typed_delta() -> None:
    rows = [
        {"arm": "baseline", "ok": True, "analysis": {"accepted": True, "expected_action": False, "strict_single_json": True, "clean_repair": False}},
        {"arm": "typed", "ok": True, "analysis": {"accepted": True, "expected_action": True, "strict_single_json": True, "clean_repair": True}},
    ]

    summary = probe._summarize(rows)

    assert summary["arms"]["baseline"]["clean_repair"] == 0
    assert summary["arms"]["typed"]["clean_repair"] == 1
    assert summary["typed_delta_vs_baseline"]["clean_repair"] == 1
