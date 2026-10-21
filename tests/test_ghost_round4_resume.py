from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "manual" / "ghost_memory_routing" / "round4.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("ghost_round4_resume", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_parse_indices_empty_means_full_run() -> None:
    assert module.parse_indices("") is None
    assert module.parse_indices("  ") is None


def test_parse_indices_deduplicates_and_keeps_zero() -> None:
    assert module.parse_indices("0, 14,14, 19") == {0, 14, 19}


@pytest.mark.parametrize("value", ["-1", "one", "1,,2"])
def test_parse_indices_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        module.parse_indices(value)
