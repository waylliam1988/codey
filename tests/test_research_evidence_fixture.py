"""The controlled evidence probe freezes content and ordering independently."""

import hashlib
import json
from pathlib import Path

from tests.manual.research_evidence_local import INPUTS, PASSAGES, fixture_windows


def test_windows_are_lossless_and_sampling_seed_is_not_an_input():
    text = "\n".join(start + "X" * 601 + end for start, end in PASSAGES.values())
    first = fixture_windows(text, "writer_limit", "candidates", 0)
    assert first == fixture_windows(text, "writer_limit", "candidates", 0)
    for row in first:
        excerpt = text[row["offset"]:row["offset"] + row["limit"]]
        assert hashlib.sha256(excerpt.encode()).hexdigest() == row["sha256"]
    assert {row["selector"] for row in first} == set(INPUTS["candidates"]["writer_limit"])


def test_rubric_covers_all_five_minimal_and_two_composite_cases():
    rubric = json.loads((Path(__file__).parent / "fixtures/research_local_rubric.json").read_text())
    assert len(rubric["cases"]) == 7
    assert all(case in INPUTS["given"] and row["required"] and row["forbidden"]
               for case, row in rubric["cases"].items())
