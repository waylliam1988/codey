"""Every frozen old behavior has an executable, independently isolated oracle."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.support.kernel_parity_cases import cases_for_inventory
from tools.kernel_parity import BOUNDARIES, DELTAS, LEGACY_COMMIT, ROOT, compare, load_baseline, probe, surface_diff

BASELINE = load_baseline()


@pytest.fixture(scope="module")
def current_observations():
    return probe(ROOT, BASELINE["cases"])


@pytest.mark.parametrize("case", BASELINE["cases"], ids=lambda case: case["id"])
def test_frozen_legacy_behavior_or_exact_reviewed_delta(case, current_observations):
    key = case["id"]
    deltas = json.loads(DELTAS.read_text(encoding="utf-8"))
    old, new = BASELINE["observations"][key], current_observations[key]
    if key not in deltas:
        assert new == old
    else:
        delta = deltas[key]
        assert delta["reason"] and delta["tests"] and delta["changelog"]
        assert delta["changelog"] == "CHANGELOG.md"
        assert all((ROOT / test).is_file() for test in delta["tests"])
        assert delta["before"] == old and delta["after"] == new
        assert old != new, "obsolete delta must be removed"


def test_case_matrix_cannot_silently_lose_legacy_tools_aliases_or_parameters():
    assert BASELINE["commit"] == LEGACY_COMMIT
    assert cases_for_inventory(BASELINE["inventory"]) == BASELINE["cases"]
    assert set(BASELINE["observations"]) == {case["id"] for case in BASELINE["cases"]}
    assert BASELINE["inventory"]["ast_surface"]
    assert BASELINE["inventory"]["providers"]
    assert BASELINE["inventory"]["request_fields"]


def test_allowlist_never_masks_a_new_unclassified_difference():
    current = copy.deepcopy(BASELINE["observations"])
    key = "protocol/coding/coding_writer/json/read_file/example-0"
    current[key] = {"accepted": False, "calls": [], "done": None}
    report = compare(BASELINE, current, {})
    assert report["failures"] == [{"id": key, "kind": "unclassified_difference",
                                   "before": BASELINE["observations"][key], "after": current[key]}]


def test_allowlist_requires_exact_after_and_rejects_stale_or_unknown_rows():
    key = "protocol/coding/coding_writer/json/read_file/example-0"
    report = compare(BASELINE, BASELINE["observations"], {
        key: {"reason": "explanation", "before": BASELINE["observations"][key], "after": None},
        "unknown": {"reason": "explanation", "before": None, "after": None},
    })
    assert [row["kind"] for row in report["failures"]] == ["stale_delta", "unknown_delta"]


def test_removed_modules_and_every_legacy_request_field_have_explicit_dispositions():
    boundary = json.loads(BOUNDARIES.read_text(encoding="utf-8"))
    current = probe(ROOT)
    surface = surface_diff(BASELINE["inventory"], current)
    assert set(surface["removed_modules"]) == set(boundary["removed_modules"])
    for row in boundary["removed_modules"].values():
        assert row["current_owners"] and row["tests"]
        assert all((ROOT / path).is_file() for path in row["current_owners"] + row["tests"])
    fields = [field for fields in boundary["request_dispositions"].values() for field in fields]
    assert len(fields) == len(set(fields))
    assert set(fields) == set(BASELINE["inventory"]["request_fields"])


def test_baseline_export_rejects_extra_source_outside_the_pinned_git_tree(monkeypatch, tmp_path):
    import tools.kernel_parity as runner

    blob = b"x = 1\n"
    inv = copy.deepcopy(BASELINE["inventory"])
    inv["source_hashes"] = {"codey/a.py": hashlib.sha256(blob).hexdigest(), "codey/extra.py": "not-pinned"}
    monkeypatch.setattr(runner, "probe", lambda *_a, **_k: inv)
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **_k: SimpleNamespace(
        stdout="100644 blob abc\tcodey/a.py\n" if command[1] == "ls-tree" else blob))
    with pytest.raises(RuntimeError, match="source file set"):
        runner.export_baseline(tmp_path, tmp_path / "baseline.json")


def test_coding_parity_scripts_never_synthesize_done_after_replies_exhausted():
    from tests.support import kernel_parity_probe

    for case in BASELINE["cases"]:
        if case["boundary"] == "coding_loop":
            kernel_parity_probe.coding_loop(case, legacy=False)


def test_research_parity_scripts_never_synthesize_done_after_replies_exhausted():
    from tests.support import kernel_parity_probe

    for case in BASELINE["cases"]:
        if case["boundary"] == "research_loop":
            kernel_parity_probe.research_loop(case, legacy=False)


def test_parity_provider_rejects_exhausted_script_instead_of_synthesizing_done():
    from tests.support.kernel_parity_probe import ScriptedProvider

    provider = ScriptedProvider({"id": "test/exhausted", "replies": []})
    with pytest.raises(RuntimeError, match="script exhausted"):
        provider.send("prompt")


def test_parity_probe_restores_native_tools_environment_after_native_case(monkeypatch):
    import os

    from tests.support import kernel_parity_probe

    monkeypatch.setenv("NATIVE_TOOLS", "sentinel")
    case = next(row for row in BASELINE["cases"] if row["id"] == "loop/native-read-done")
    kernel_parity_probe.coding_loop(case, legacy=False)
    assert os.environ["NATIVE_TOOLS"] == "sentinel"
