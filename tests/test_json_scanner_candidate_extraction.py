"""Candidate extraction preserves JSON string content and strict decoding."""

from __future__ import annotations

from codey.protocols.json_scanner import extract_candidate_json_objects


def test_think_tags_inside_json_strings_are_preserved() -> None:
    objects = extract_candidate_json_objects(
        '{"tool":"edit","args":{"content":"<think>keep this</think>"}}'
    )

    assert objects == [{"tool": "edit", "args": {"content": "<think>keep this</think>"}}]


def test_think_block_outside_json_is_removed_before_candidate_lookup() -> None:
    objects = extract_candidate_json_objects(
        '<think>internal reasoning</think>\n{"tool":"read_file","args":{"path":"a.py"}}'
    )

    assert objects == [{"tool": "read_file", "args": {"path": "a.py"}}]


def test_unescaped_control_character_is_rejected_by_candidate_lookup() -> None:
    objects = extract_candidate_json_objects('{"tool":"edit","args":{"content":"first\nsecond"}}')

    assert objects == []
