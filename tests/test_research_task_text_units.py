"""Original task text offsets and sentence boundaries remain intact."""

import pytest

from codey.research.task_requirements import task_requirements, text_units


@pytest.mark.parametrize("text", [
    "Compare current guarantees, old limits; measure workload.\nReport in Chinese.",
    "\u5f53\u524d\u4fdd\u8bc1\uff0c\u65e7\u7248\u672c\u9650\u5236\uff0c\u5de5\u4f5c\u8d1f\u8f7d\u3002\n\u4e2d\u6587\u7ed3\u8bba\u3002",
    "Read https://www.sqlite.org/wal.html and v3.22.0. Preserve 0.5 ms.",
])
def test_text_scope_covers_every_nonblank_character_and_keeps_original_offsets(text):
    units = text_units(text)
    covered = set()
    for row in units:
        assert row["text"] == text[row["start"]:row["end"]]
        covered.update(range(row["start"], row["end"]))
    assert all(index in covered for index, char in enumerate(text) if not char.isspace())
    assert task_requirements(text) == task_requirements(text)
    assert [row["task_ref"] for row in task_requirements(text)] == [f"r{n}" for n in range(1, len(units) + 1)]
    assert any("https://www.sqlite.org/wal.html" in row["text"] for row in units) == ("https://" in text)


@pytest.mark.parametrize("text", [
    "在 WAL 模式下，同一时刻只能有一个写者。",
    "Before 3.22.0, read-only opening was impossible; since 3.22.0, specific conditions apply.",
    "In mice, A beat B, but C was not evaluated.",
])
def test_sentence_keeps_conditions_subject_and_comparator_together(text):
    units = text_units(text, sentences=True)
    assert units == [{"unit": 1, "start": 0, "end": len(text), "text": text}]
    assert len(task_requirements(text)) > 1


@pytest.mark.parametrize("text", [
    "原文说明：‘There can only be one writer at a time.’（同一时刻只有一个写者。）",
    'The documentation says "Only one writer."',
    "说明：“一个写者。”",
])
def test_closing_punctuation_stays_with_its_sentence_and_original_offsets(text):
    units = text_units(text, sentences=True)
    assert units == [{"unit": 1, "start": 0, "end": len(text), "text": text}]
