from __future__ import annotations

from pathlib import Path

from tests.manual import project_map_symbol_ab, scoped_task_plan_ab

ROOT = Path(__file__).resolve().parents[1]


def test_project_map_json_protocol_case_targets_current_production_files() -> None:
    case = next(item for item in project_map_symbol_ab.CASES if item.name == "json-parallel-protocol")

    assert case.expected_paths == (
        "codey/protocols/text_turn.py",
        "codey/operations/kernel_protocol.py",
        "tests/test_text_turn_decoder_contract.py",
    )
    assert all((ROOT / path).is_file() for path in case.expected_paths)


def test_scoped_json_batching_case_has_no_retired_codec_target() -> None:
    case = scoped_task_plan_ab.cases()["json-tool-batching"]

    assert case.expected_paths == (
        "codey/protocols/text_turn.py",
        "codey/operations/kernel_protocol.py",
    )
    assert case.expected_tests == (
        "tests/test_text_turn_decoder_contract.py",
        "tests/test_protocols.py",
    )
    assert case.expected_terms == ("parallel", "read_files", "TextRequests", "normalize_turn")
    assert all((ROOT / path).is_file() for path in (*case.expected_paths, *case.expected_tests))
