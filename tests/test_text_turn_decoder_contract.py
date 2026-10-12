"""Contract tests for the text protocol boundary.

These tests describe the existing text behavior before moving decoding out of
the kernel: ordered JSON requests, whole-batch rejection, direct answers, and
the existing protocol diagnostics.
"""

from __future__ import annotations

import json

from codey.protocols.text_turn import TextAnswer, TextError, TextRequests, decode_text_turn


def _request(tool: str, args: dict[str, object]) -> str:
    return json.dumps({"tool": tool, "args": args}, ensure_ascii=False)


def test_decoder_preserves_order_and_batch_members() -> None:
    decoded = decode_text_turn(
        "\n".join(
            (
                _request("read_file", {"path": "a.py"}),
                _request("read_file", {"path": "b.py"}),
            )
        )
    )

    assert isinstance(decoded, TextRequests)
    assert [item.tool for item in decoded.items] == ["read_file", "read_file"]
    assert [item.args["path"] for item in decoded.items] == ["a.py", "b.py"]


def test_decoder_rejects_invalid_member_as_a_whole_batch() -> None:
    valid = _request("read_file", {"path": "a.py"})
    invalid = '{"tool":"read_file","args":{"path":"b.py\n"}}'

    decoded = decode_text_turn(valid + "\n" + invalid)

    assert isinstance(decoded, TextError)
    assert decoded.kind == "invalid_json"
    assert "escape" in decoded.message.lower()


def test_decoder_preserves_direct_answers_and_fenced_examples() -> None:
    answer = decode_text_turn("I checked the file.")
    fenced = decode_text_turn('```json\n{"tool":"read_file","args":{"path":"a.py"}}\n```')

    assert isinstance(answer, TextAnswer)
    assert answer.text == "I checked the file."
    assert isinstance(fenced, TextAnswer)
    assert fenced.text.startswith("```json")


def test_decoder_delivers_json_data_without_turning_it_into_a_request() -> None:
    decoded = decode_text_turn('{"example": {"tool": "read_file"}}')

    assert isinstance(decoded, TextAnswer)
    assert decoded.text == '{"example": {"tool": "read_file"}}'


def test_decoder_keeps_multiple_json_data_objects_as_the_existing_protocol_error() -> None:
    decoded = decode_text_turn('{"a":1}\n{"b":2}')

    assert isinstance(decoded, TextError)
    assert decoded.kind == "invalid_json"
    assert decoded.message == "incomplete or malformed JSON response"
