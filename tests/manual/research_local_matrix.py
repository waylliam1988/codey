"""Sequential local-model matrices; preserve every failure for manual grading.

All cells use the same production kernel and local submission contract. The
factorial matrix changes relationship splitting and history projection only;
the current work packet, source snapshot, ordering and sampling stay fixed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from tests.manual.research_evidence_local import DEFAULT_CLOCK_FILE, fixture_clock

SEMANTIC_AXES = ("condition_or_relation_omission", "unsupported_addition", "state_misreport", "gap_termination")


def pending_review():
    """No lexical scoring: fill these axes by checking original evidence/calls."""
    return {axis: {"status": "pending_manual_review", "evidence": []} for axis in SEMANTIC_AXES}


def matrix(suite: str) -> list[dict]:
    cases = ("checkpoint_stop", "checkpoint_resume") if suite == "relations" else (
        ("versions", "checkpoint_constraints") if suite == "factorial" else (
        "writer_limit", "readonly_conditions", "checkpoint_stop", "checkpoint_resume", "coexistence_only"))
    configurations = (("A", False, True), ("B", True, True), ("C", False, False), ("D", True, False)) if suite == "factorial" else (("local", False, False),)
    if suite == "relations":
        configurations = (("split", True, False),)
    return [{"case": case, "seed": seed, "order_seed": 0, "configuration": label,
             "split": split, "full_history": full_history,
             "layer": "candidates" if suite == "candidates" else "given"}
            for label, split, full_history in configurations for case in cases for seed in (74, 75, 76)]


def wire_stats(directory: Path) -> dict:
    """Measure admitted payloads and server usage without semantic auto-grading."""
    wire = directory / "research-wire.jsonl"
    if not wire.is_file():
        return {"wire_missing": True}
    payload_characters, prompt_tokens, tools, clock_counts, clock_hashes = [], [], {}, [], set()
    for line in wire.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("event") == "admitted_payload":
            payload_characters.append(len(json.dumps(row["payload"], ensure_ascii=False)))
            payload = row["payload"]
            messages = payload.get("messages", payload.get("input", []))
            clocks = [message["content"] for message in messages if isinstance(message, dict)
                      and isinstance(message.get("content"), str) and message["content"].startswith("<request_context>")]
            clock_counts.append(len(clocks))
            clock_hashes.update(hashlib.sha256(clock.encode()).hexdigest() for clock in clocks)
        if row.get("event") == "generation_response":
            body = row["body"]
            prompt_tokens.append(body.get("usage", {}).get("prompt_tokens"))
            try:
                call = json.loads(body["choices"][0]["message"]["content"])
                name = call["tool"]
            except (KeyError, IndexError, TypeError, ValueError):
                name = "unparsed_response"
            tools[name] = tools.get(name, 0) + 1
    return {"request_count": len(payload_characters), "admitted_payload_characters": payload_characters,
            "prompt_tokens": prompt_tokens, "total_prompt_tokens": (
                sum(prompt_tokens) if prompt_tokens and all(type(n) is int for n in prompt_tokens) else None),
            "model_tool_counts": tools, "clock_observation_counts": clock_counts,
            "clock_sha256_values": sorted(clock_hashes)}


def initial_exchange(directory):
    """Hash the actual first input/output, preserving schema field order too."""
    path = directory / "research-wire.jsonl"
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        event = row.get("event")
        if event == "admitted_payload" and "payload" not in values:
            values["payload"] = row["payload"]
        elif event == "generation_response" and "response" not in values:
            values["response"] = row["body"]["choices"][0]["message"]
        if len(values) == 2:
            break
    return {key: hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            for key, value in values.items()}


def initial_pair_comparisons(runs):
    """Equal seeds are labels, not a backend determinism guarantee."""
    indexed = {(row["configuration"], row["case"], row["seed"]): row for row in runs}
    comparisons = []
    for row in runs:
        partner = {"A": "C", "B": "D"}.get(row["configuration"])
        other = indexed.get((partner, row["case"], row["seed"]))
        if other is None:
            continue
        exchanges = [initial_exchange(Path(item["directory"])) for item in (row, other)]
        comparisons.append({"case": row["case"], "seed": row["seed"], "configurations": [row["configuration"], partner],
            "exchanges": exchanges,
            "same_payload": "payload" in exchanges[0] and exchanges[0].get("payload") == exchanges[1].get("payload"),
            "same_response": "response" in exchanges[0] and exchanges[0].get("response") == exchanges[1].get("response")})
    return comparisons


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-file", type=Path, required=True)
    parser.add_argument("--clock-file", type=Path, default=DEFAULT_CLOCK_FILE)
    parser.add_argument("--suite", choices=("minimal", "factorial", "candidates", "relations"), required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    clock = fixture_clock(args.clock_file)
    clock_hash = hashlib.sha256(clock.encode()).hexdigest()
    rubric_path = Path(__file__).resolve().parents[1] / "fixtures/research_local_rubric.json"
    rubric_bytes = rubric_path.read_bytes()
    (args.directory / "rubric.json").write_bytes(rubric_bytes)
    runs = matrix(args.suite)
    index = {"suite": args.suite, "semantic_grading": "manual_required",
             "model_visible_clock": {"text": clock, "sha256": clock_hash},
             "rubric_sha256": hashlib.sha256(rubric_bytes).hexdigest(), "runs": runs}
    path = args.directory / "matrix.json"
    for row in runs:
        name = f"{row['configuration']}-{row['case']}-{row['seed']}"
        directory = args.directory / name
        command = [sys.executable, "-X", "utf8", "-m", "tests.manual.research_evidence_local",
            "--directory", str(directory), "--source-file", str(args.source_file),
            "--clock-file", str(args.clock_file.resolve()),
            "--case", row["case"], "--layer", row["layer"], "--seed", str(row["seed"]),
            "--order-seed", str(row["order_seed"]), "--max-turns", "12", "--provider-timeout", "120",
            *(["--split"] if row["split"] else []), *(["--full-history"] if row["full_history"] else [])]
        row["directory"] = str(directory.resolve())
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        started = time.monotonic()
        print(f"START {name}", flush=True)
        with (args.directory / f"{name}.log").open("w", encoding="utf-8") as log:
            try:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=900)
                row["process_exit_code"] = process.returncode
            except subprocess.TimeoutExpired:
                row["runner_timeout"] = True
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
        result = directory / "research-result.json"
        if result.is_file():
            row["result"] = json.loads(result.read_text(encoding="utf-8"))
        row["wire_stats"] = wire_stats(directory)
        stats = row["wire_stats"]
        row["clock_matches_fixture"] = (stats.get("clock_sha256_values") == [clock_hash]
            and bool(stats.get("clock_observation_counts")) and all(count == 1 for count in stats["clock_observation_counts"]))
        row["semantic_grade"] = "pending_manual_review"
        row["semantic_review"] = pending_review()
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"END {name}: {row.get('result', {}).get('stop_reason', 'runner_failure')} ({row['elapsed_seconds']}s)", flush=True)
    index["initial_request_comparisons"] = initial_pair_comparisons(runs)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
