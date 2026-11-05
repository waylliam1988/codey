"""Recheck archived final files, including generated tests, without a model."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.manual.coding_memory_value_ab import followup
from tests.manual.coding_pipeline_cases import BY_NAME
from tests.manual.coding_pipeline_value_ab import grade, save, source_metadata, write_files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = {"method": "offline final files; no provider calls; raw runs unmodified", "batches": {}, **source_metadata()}
    for path in args.input:
        original = json.loads(path.read_text(encoding="utf-8"))
        histories = {row["case"]: row for row in original.get("histories", [])}
        rows = []
        for index, row in enumerate(original["rows"]):
            case = BY_NAME.get(row["case"])
            if case is None:
                name = "discount-bug" if row["case"] == "basket-continuation" else "cross-file-rename"
                case = followup(name, histories[name]["files"])
            with tempfile.TemporaryDirectory() as temporary:
                assessment_root = Path(temporary)
                write_files(assessment_root, row["files"])
                assessment = grade(assessment_root, case)
                assessment.pop("files")
            rows.append({"index": index, "case": row["case"], "arm": row["arm"],
                "phase": row.get("phase"), "seed": row.get("seed"), "stop_reason": row.get("stop_reason"),
                "original_success": row["success"], **assessment})
        payload["batches"][path.name] = rows
        save(args.output, payload)
        print(json.dumps({"batch": path.name, "rows": len(rows),
            "behavior_passed": sum(row["product_behavior_passed"] for row in rows),
            "visible_suite_passed": sum(row["visible_suite"]["ok"] for row in rows),
            "quality_passed": sum(row["quality_passed"] for row in rows)}), flush=True)


if __name__ == "__main__":
    main()
