"""Execution facts and model formatting are separate diagnostic results."""
from __future__ import annotations

from codey.types.json_value import JsonObject
from tools.shell_web_encoding_gate import analyze_reply


def test_actual_encoding_annotation_is_factual_but_not_exact_format() -> None:
    facts: JsonObject = {"encoding": {"stderr": "[encoding: gbk]\n中文错误"}}
    expected: JsonObject = {"native": {"exit_code": 7, "exit_code_scope": "process"}, "chinese_error": "中文错误"}
    actual: JsonObject = {**expected, "chinese_error": "[encoding: gbk]\n中文错误"}
    analysis = analyze_reply(facts, actual, expected)
    assert analysis["ok"] is True and analysis["exact_format"] is False
    assert not analyze_reply(facts, {**actual, "chinese_error": "乱码"}, expected)["ok"]


def test_wrong_scope_and_boolean_exit_cannot_pass() -> None:
    facts: JsonObject = {"encoding": {"stderr": "中文错误"}}
    expected: JsonObject = {"native": {"exit_code": 1, "exit_code_scope": "process"}, "chinese_error": "中文错误"}
    for wrong in ({"exit_code": 1, "exit_code_scope": "shell"}, {"exit_code": True, "exit_code_scope": "process"}):
        actual: JsonObject = {**expected, "native": wrong}
        assert not analyze_reply(facts, actual, expected)["ok"]
