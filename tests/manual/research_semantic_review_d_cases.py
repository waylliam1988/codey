"""Frozen natural Research answers and source evidence for D revision experiment."""

from __future__ import annotations

import json
from pathlib import Path

from codey.types.json_value import JsonObject, decode_json_object

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "research_semantic_review_d.json"


def load_cases() -> list[JsonObject]:
    """Load the original drafts, evidence and rubrics without local run artifacts."""
    return [decode_json_object(row) for row in json.loads(FIXTURE.read_text(encoding="utf-8"))]
