"""Leakage, retrieval and independent-grader checks for the manual live pilot."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.research.ledger import ResearchLedger
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from tools.agent_work_graph_probe import (
    ARMS,
    CONTEXT_CHARS,
    RETRY_NEW,
    Case,
    Node,
    build_graph,
    cases,
    evaluate,
    retrieve,
)


def fixture(directory: Path, name: str) -> Case:
    case = cases()[name]
    for key, body in case.files.items():
        path = directory / "project" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return case


def test_graph_comes_from_visible_files_not_unopened_bodies(tmp_path: Path) -> None:
    case = fixture(tmp_path, "relay")
    nodes, edges = build_graph(tmp_path / "project")
    assert nodes[RETRY_NEW]["kind"] == "source_locator"
    assert "2..17" not in json.dumps(nodes)
    assert require_json_string(case.pages[RETRY_NEW]["text"], "source text") not in json.dumps(nodes)
    assert ("relay/client.py", "imports", "relay/rules.py") in edges
    assert ("docs/releases.md", "cites", RETRY_NEW) in edges


def test_only_committed_source_body_enters_graph(tmp_path: Path) -> None:
    fixture(tmp_path, "relay")
    ledger = ResearchLedger()
    ledger.record_open(RETRY_NEW, RETRY_NEW, "Source", "committed observation")
    nodes, _ = build_graph(tmp_path / "project", ledger)
    assert nodes[RETRY_NEW]["text"] == "committed observation"
    assert nodes[RETRY_NEW]["kind"] == "read_source"


def test_failures_do_not_become_successful_evidence(tmp_path: Path) -> None:
    fixture(tmp_path, "relay")
    observations: list[JsonObject] = [{"kind": "fetch", "url": RETRY_NEW,
        "page": {"status": "error", "failure_code": "login_required", "text": "uncommitted body"}}]
    nodes, _ = build_graph(tmp_path / "project", observations=observations)
    assert nodes[RETRY_NEW]["kind"] == "failed_source"
    assert "uncommitted body" not in json.dumps(nodes)


def test_graph_refreshes_versions_after_edits(tmp_path: Path) -> None:
    fixture(tmp_path, "checkout")
    project = tmp_path / "project"
    before, _ = build_graph(project)
    (project / "shop/amounts.py").write_text("# changed\n", encoding="utf-8")
    after, _ = build_graph(project)
    assert before["shop/amounts.py"]["sha256"] != after["shop/amounts.py"]["sha256"]


def test_graph_expands_structural_neighbor_over_unrelated_text() -> None:
    nodes: dict[str, Node] = {"entry.py": {"kind": "file", "text": "needle entry", "sha256": ""},
        "noise.md": {"kind": "file", "text": "needle unrelated", "sha256": ""},
        "impl.py": {"kind": "file", "text": "implementation", "sha256": ""}}
    edges = [("entry.py", "imports", "impl.py")]
    text, selected = retrieve(nodes, edges, "needle entry", "graph")
    assert "impl.py" in selected
    assert "--imports-->" in text


@pytest.mark.parametrize("arm", ARMS)
def test_context_budget_and_empty_inputs(tmp_path: Path, arm: str) -> None:
    fixture(tmp_path, "relay")
    nodes, edges = build_graph(tmp_path / "project")
    text, selected = retrieve(nodes, edges, cases()["relay"].task, arm)
    assert len(text) <= CONTEXT_CHARS
    assert all(key in nodes for key in selected)
    if arm == "baseline":
        assert not text and not selected
    empty, _ = retrieve({}, [], "", arm)
    assert len(empty) <= CONTEXT_CHARS


def test_invalid_arm_rejected() -> None:
    with pytest.raises(ValueError):
        retrieve({}, [], "", "oracle")


@pytest.mark.parametrize("name", ["checkout", "relay"])
def test_independent_grader_rejects_original_and_accepts_reference(tmp_path: Path, name: str) -> None:
    case = fixture(tmp_path, name)
    original = evaluate(tmp_path, case)
    assert not original["success"]
    assert not all(decode_json_object(original["product_checks"]).values())
    if name == "checkout":
        body = (
            "from decimal import Decimal, ROUND_HALF_UP\n\n"
            "def settle(subtotal, discount, rate, channel):\n"
            "    net = max(subtotal-discount, Decimal('0'))\n"
            "    value = net if channel == 'wholesale' else net*(1+rate)\n"
            "    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)\n"
        )
    else:
        body = (
            "def retry_delay(status, method, retry_after):\n"
            "    if status not in (429,503) or method not in ('GET','HEAD'):\n"
            "        return None\n"
            "    if not isinstance(retry_after, str):\n"
            "        return 4\n"
            "    text = retry_after.strip()\n"
            "    if not text or not text.isascii() or not text.isdecimal():\n"
            "        return 4\n"
            "    return min(max(int(text),2),17)\n"
        )
        (tmp_path / "acquisitions.jsonl").write_text(json.dumps({"kind": "fetch", "url": RETRY_NEW,
            "page": {"status": "ok"}}) + "\n", encoding="utf-8")
    (tmp_path / "project" / case.allowed_changes[0]).write_text(body, encoding="utf-8")
    result = evaluate(tmp_path, case)
    assert result["success"], result
    (tmp_path / "project" / "tests/test_smoke.py").write_text("# removed assertions\n", encoding="utf-8")
    assert not evaluate(tmp_path, case)["success"]
