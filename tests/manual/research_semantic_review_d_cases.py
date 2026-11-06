"""Frozen natural Research answers and source evidence for D revision experiment."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(".e2e-artifacts/research-fidelity-source-ab-v2-20261104")
BASE_CASES = ("error_complex", "version_scope", "condition_scope", "known_url", "two_errors", "source_discovery")


def load_cases():
    fixtures = {row["id"]: row for row in json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8"))["cases"]}
    result = []
    for identity in BASE_CASES:
        fixture = fixtures[identity]
        report_path = ROOT / "runs" / f"{identity}-641-baseline" / "research-report.txt"
        report = report_path.read_text(encoding="utf-8")
        evidence = [{"label": f"e{i}", "source_id": f"{identity}-s{i}", "title": doc["title"],
                     "url": doc["url"], "excerpt": doc["text"], "content_kind": "html"}
                    for i, doc in enumerate(fixture["documents"], 1)]
        result.append({"id": identity, "question": fixture["question"], "draft": report,
                       "evidence": evidence, "source_report": str(report_path),
                       "split": "frozen_natural", "gold": _gold(identity)})
    return result


def _gold(identity):
    common = {"required": [], "forbidden": [], "citation_labels": []}
    if identity in {"known_url", "two_errors", "source_discovery"}:
        common["required"] = ["3.22.0", "-shm", "-wal", "writable", "immutable"]
        common["forbidden"] = ["key alone", "必须同时满足"]
        common["citation_labels"] = ["e1"]
    elif identity == "condition_scope":
        common["required"] = ["21", "28", "且", "或"]
        common["forbidden"] = ["R4.*或.*audit", "R5.*且"]
        common["citation_labels"] = ["e1", "e2"]
    elif identity == "version_scope":
        common["required"] = ["V7", "V8", "9", "13", "5", "6"]
        common["forbidden"] = ["V7.*只读.*支持", "V8.*不支持"]
        common["citation_labels"] = ["e1", "e2"]
    else:
        common["required"] = ["C1", "C2", "7", "11", "ARROW", "BIRCH", "INDEX-P"]
        common["forbidden"] = ["本机.*更快", "证明.*速度"]
        common["citation_labels"] = ["e1", "e2"]
    return common
