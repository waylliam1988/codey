"""Prepare a fresh 4B formal fixture set from natural 2A baseline answers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

from tests.manual.research_experiment_identity import freeze_experiment

SOURCE_ROOT = Path(".e2e-artifacts/research-live-web-2a-formal-v3")


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def extract(folder):
    values = read(folder / "research-knowledge.json")
    answer = next(row for row in values if row["kind"] == "answer")
    evidence = {row["id"]: row for row in answer["referenced_versions"] if row["kind"] == "evidence"}
    packets = []
    for binding in answer["provenance"].get("evidence_bindings", []):
        item = evidence[binding["evidence_id"]]
        source_id = next(ref["target"] for ref in item["references"] if ref["relation"] == "from_source")
        source = next(row for row in answer["provenance"].get("source_versions", []) if row["id"] == source_id)
        packets.append({"label": binding["label"], "source_id": source_id,
                        "revision": binding["evidence_revision"], "start": binding["start"],
                        "end": binding["end"], "excerpt": binding["excerpt"],
                        "url": source["url"], "title": source["title"]})
    return {"task": answer["provenance"]["task"], "draft": answer["content"],
            "evidence": packets, "draft_digest": answer["provenance"]["submission_digest"]}


def prepare(root):
    root.mkdir(parents=True, exist_ok=False)
    folders = sorted(SOURCE_ROOT.glob("*-baseline"))
    if len(folders) != 12:
        raise RuntimeError(f"expected 12 natural baseline drafts, found {len(folders)}")
    fixtures = {folder.name.removesuffix("-baseline"): extract(folder) for folder in folders}
    with urlopen("http://127.0.0.1:5001/v1/models", timeout=10) as response:
        model = read_json(response)["data"][0]["id"]
    write(root / "fixtures.json", fixtures)
    write(root / "contract.json", {
        "phase": "formal-v1", "cases": list(fixtures), "arms": ["U", "R", "S", "V"],
        "seeds": [1801, 1802], "model": model,
        "design": "12 natural drafts from a separate completed 2A baseline, exact evidence bindings and labels retained; U/R/S/V use direct submit, direct revision, self review plus revision, independent evidence review plus revision.",
        "gate": {"positive_tasks_min": 3, "negative_tasks_max": 1, "strict_success_delta_min": 0.10,
                 "new_severe_regressions_max": 0, "cost_ratio_max": 1.25},
    })
    write(root / "identity.json", {"contract_sha256": hashlib.sha256((root / "contract.json").read_bytes()).hexdigest(),
                                    "source_sha256": {"tests/manual/research_revision_4b_matrix_v5.py": hashlib.sha256(Path("tests/manual/research_revision_4b_matrix_v5.py").read_bytes()).hexdigest(),
                                                      "tests/manual/persistent_research_local.py": hashlib.sha256(Path("tests/manual/persistent_research_local.py").read_bytes()).hexdigest()}})
    freeze_experiment(root)


def read_json(response):
    return json.loads(response.read().decode("utf8"))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    prepare(parser.parse_args().root.resolve())
