"""Keep the pilot scorer conservative about code syntax and missing answers."""

import pytest

from codey.types.json_value import JsonObject
from tools.memory_continuity_probe import backend_idle, score


@pytest.mark.parametrize("expected,text,passed", [
    ("rust", 'fn main() { println!("Hello"); }', True),
    ("rust", 'print("Hello")', False),
    ("rust", "Rust is a language", False),
    ("python", 'print("Hello")', True),
    ("python", 'fn main() { println!("Hello"); }', False),
    ("spanish", "Una prueba comprueba una función.", True),
    ("spanish", "A unit test checks a function.", False),
    ("history", "SQLite works without a separate server.", True),
    ("history", "SQLite", False),
    ("history", "I cannot recall that decision.", False),
    ("python", "", False),
])
def test_frozen_behavior_proxy(expected: str, text: str, passed: bool) -> None:
    assert score(expected, text) is passed


@pytest.mark.parametrize("payload,expected", [
    ({"idle": 1, "queue": 0}, True), ({"idle": True, "queue": 0}, True),
    ({"idle": 0, "queue": 0}, False), ({"idle": 1, "queue": 1}, False),
    ({"idle": "1", "queue": 0}, False), ({}, False),
])
def test_backend_preflight_accepts_documented_integer_flags(payload: JsonObject, expected: bool) -> None:
    assert backend_idle(payload) is expected
