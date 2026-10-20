from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parent
    / "manual"
    / "ghost_memory_routing"
    / "evaluation_contract.py"
)
SPEC = importlib.util.spec_from_file_location("ghost_evaluation_contract", MODULE_PATH)
assert SPEC and SPEC.loader
contract = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = contract
SPEC.loader.exec_module(contract)


def _entry(runs: int, passed: int) -> dict[str, int]:
    return {"runs": runs, "passed": passed}


def _cells(*, suite: str = "modelA", runs: int = 20) -> dict:
    return {
        "lang_on_d2": {suite: {"lang": _entry(runs, 19)}},
        "lang_off_d2": {suite: {"lang": _entry(runs, 0)}},
        "cmd_on_d2": {suite: {"cmd": _entry(runs, 12)}},
        "cmd_off_d2": {suite: {"cmd": _entry(runs, 0)}},
    }


def test_frozen_config_records_single_model_pilot_and_sample_floor() -> None:
    config = contract.frozen_config()
    assert config["required_suites"] == ["modelA"]
    assert config["minimum_runs_per_cell"] == 20
    assert config["default_prompt_changed"] is False


def test_missing_control_or_sample_is_incomplete() -> None:
    cells = _cells()
    cells["lang_off_d2"]["modelA"]["lang"]["runs"] = 19
    verdicts = contract.family_verdicts(cells)
    assert verdicts["_coverage"]["status"] == "INCOMPLETE"
    assert verdicts["language"]["verdict"] == "INCOMPLETE"


def test_promote_matched_is_aggregated_as_promote() -> None:
    verdicts = contract.family_verdicts(_cells())
    assert verdicts["command"]["verdict"] == "PROMOTE-MATCHED"
    assert verdicts["command"]["rank"] == 2


def test_missing_suite_is_incomplete_instead_of_zero() -> None:
    verdicts = contract.family_verdicts(_cells(suite="modelB"))
    assert verdicts["_coverage"]["status"] == "INCOMPLETE"
    assert verdicts["language"]["verdict"] == "INCOMPLETE"
