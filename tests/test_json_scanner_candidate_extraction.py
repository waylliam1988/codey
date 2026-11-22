"""Candidate extraction preserves JSON string content and strict decoding."""

from __future__ import annotations

import json
import math

from codey.protocols.json_scanner import extract_candidate_json_objects, extract_json_objects


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


def test_general_extraction_keeps_its_permissive_control_character_mode() -> None:
    objects = extract_json_objects('{"content":"first\nsecond"}')

    assert objects == [{"content": "first\nsecond"}]


def test_invalid_balanced_fragments_do_not_hide_later_valid_objects() -> None:
    source = 'before {not JSON} after {"ok":1}'

    assert extract_json_objects(source) == [{"ok": 1}]
    assert extract_candidate_json_objects(source) == [{"ok": 1}]


def test_extraction_preserves_existing_number_and_duplicate_key_semantics() -> None:
    source = '{"value":0,"value":3,"literal":NaN}'
    for objects in (extract_json_objects(source), extract_candidate_json_objects(source)):
        assert len(objects) == 1
        assert objects[0]["value"] == 3
        literal = objects[0]["literal"]
        assert isinstance(literal, float) and math.isnan(literal)


def test_extraction_keeps_decodable_nested_values_without_a_new_depth_policy() -> None:
    source = '{"value":' + '[' * 70 + '0' + ']' * 70 + '}'
    for objects in (extract_json_objects(source), extract_candidate_json_objects(source)):
        assert len(objects) == 1
        assert json.dumps(objects[0], separators=(",", ":")) == source
