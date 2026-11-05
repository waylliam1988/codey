"""Frozen held-out pairs use ordinary entry and keep grading outside its input."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.manual.research_reduction_local import load_cases, task_for_case

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/research_reduction_cases.json"


def _frozen_fixture_sha256(path):
    # These pre-inference rubrics recorded a CRLF checkout. Reconstruct only
    # Git's newline translation, preserving every other byte and the old rubric.
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("fixture,rubric_name", [
    ("research_reduction_cases.json", "research-reduction-heldout-rubric-20261101.json"),
    ("research_whole_expanded_cases.json", "research-whole-expanded-rubric-20261101.json"),
])
def test_frozen_fixture_hash_accepts_checkout_newlines_but_rejects_content_changes(tmp_path, fixture, rubric_name):
    source = ROOT / "tests/fixtures" / fixture
    expected = json.loads((ROOT / "docs/reports" / rubric_name).read_text(encoding="utf-8"))["fixture_sha256"]
    lf = source.read_bytes().replace(b"\r\n", b"\n")
    copy = tmp_path / fixture
    for raw in (lf, lf.replace(b"\n", b"\r\n")):
        copy.write_bytes(raw)
        assert _frozen_fixture_sha256(copy) == expected
    copy.write_bytes(lf.replace(b'"question"', b'"changed_question"', 1))
    assert _frozen_fixture_sha256(copy) != expected


def test_heldout_pairs_keep_same_question_complete_alternatives_and_private_rubric():
    cases = load_cases(FIXTURES)
    rubric = json.loads((ROOT / "docs/reports/research-reduction-heldout-rubric-20261101.json").read_text(encoding="utf-8"))
    assert _frozen_fixture_sha256(FIXTURES) == rubric["fixture_sha256"]
    assert len(cases) == 6
    for topic in ("queue", "tempfile", "rlock"):
        direct, adjacent = cases[topic + "-direct"], cases[topic + "-adjacent"]
        assert direct["question"] == adjacent["question"]
        for case in (direct, adjacent):
            prompt = task_for_case(case)
            assert case["id"] not in prompt and "label" not in prompt and "required" not in prompt
            assert case["documents"][0]["text"] not in prompt  # Must actually call the source tool.
            assert case["documents"][0]["url"] in prompt
    text = cases["tempfile-direct"]["documents"][0]["text"]
    assert "at least one" in text and "O_TEMPORARY" in text and "must be closed before exiting" in text
    assert "restore the recursion level" in cases["rlock-direct"]["documents"][0]["text"]


@pytest.mark.parametrize("identifier", ["../escape", "", "same"])
def test_fixture_ids_cannot_escape_or_reuse_run_directories(tmp_path, identifier):
    row = {"id": identifier, "question": "A complete question?", "documents": [{"text": "Source body"}]}
    rows = [row, row] if identifier == "same" else [row]
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps({"cases": rows}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)


def test_expanded_cases_use_the_same_frozen_material_for_both_frameworks(tmp_path):
    from tests.manual.research_framework_comparison import documents, material_text, task_text

    source = ROOT / "tests/fixtures/research_whole_expanded_cases.json"
    rubric = json.loads((ROOT / "docs/reports/research-whole-expanded-rubric-20261101.json").read_text(encoding="utf-8"))
    assert _frozen_fixture_sha256(source) == rubric["fixture_sha256"]
    (tmp_path / "fixtures.json").write_bytes(source.read_bytes())
    (tmp_path / "material-channel.json").write_text('{"mode":"read","protocol":"native"}')
    cases = load_cases(source)
    assert len(cases) == 9
    for case in cases.values():
        assert documents(tmp_path, case["id"]) == case["documents"]
        task = task_text(tmp_path, case["id"])
        assert case["question"] in task and "required" not in task
        for doc in case["documents"]:
            assert doc["text"] not in task
            assert doc["text"] in material_text(tmp_path, case["id"])
    long = cases["long-recovery-manual"]["documents"][0]["text"]
    assert long.index("## 44.") > 18000 and "seven days" in long
