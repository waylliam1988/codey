import pytest

from codey.protocols.codemode import CodeModeInputError, parse_codemode_args, parse_codemode_text


def test_text_input_reports_excessive_json_nesting() -> None:
    prefix = '{"tool":"codemode","args":{"code":"return 1;","inputs":{"nested":'
    assert parse_codemode_text(prefix + "[[null]]}}}").code == "return 1;"

    text = prefix + "[" * 5_000 + "null" + "]" * 5_000 + "}}}"
    with pytest.raises(ValueError, match="JSON nesting"):
        parse_codemode_text(text)


def test_argument_input_identifies_invalid_unicode_in_source_code() -> None:
    assert parse_codemode_args({"code": 'return "😀";'}).code == 'return "😀";'
    with pytest.raises(CodeModeInputError, match="code must contain valid Unicode"):
        parse_codemode_args({"code": 'return "' + chr(0xD800) + '";'})
