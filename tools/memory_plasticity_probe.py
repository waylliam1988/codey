"""Frozen synthetic mechanism comparisons, separate from model-quality claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from codey.memory.codec import canonical
from codey.memory.contracts import MemoryEvent, MemoryQuery
from codey.types.json_value import JsonObject, JsonValue
from tools.memory_plasticity_mechanisms import MECHANISMS, ResearchMemory

AT = "2026-11-21T00:00:00+00:00"


class Arguments(argparse.Namespace):
    evidence: Path


def corpus() -> list[JsonObject]:
    popular: list[JsonValue] = [{"symbols": ["popular"]}] * 30
    temporal: list[JsonValue] = [{"symbols": [value]} for _ in range(10) for value in ("cue", "target")]
    drift_old: list[JsonValue] = [{"symbols": ["cue", "old"]}] * 100
    drift_new: list[JsonValue] = [{"symbols": ["cue", "new"]}] * 50
    return [
        {"id": "association", "training": [{"symbols": ["cue", "target"]}] * 20,
         "queries": [{"text": "cue", "expected": "target"}]},
        {"id": "temporal", "training": popular + temporal,
         "queries": [{"text": "cue", "expected": "target"}]},
        {"id": "delayed", "training": [{"symbols": ["cue", "useful"]}, {"symbols": ["noise"]}, {"symbols": [], "reward": 1.0}],
         "queries": [{"text": "cue", "expected": "useful"}]},
        {"id": "drift", "training": drift_old + drift_new,
         "queries": [{"text": "cue", "expected": "new"}]},
        {"id": "context_collision", "training": [{"symbols": ["cue", value], "context": context} for _ in range(10) for context, value in (("a", "alpha"), ("b", "beta"))],
         "queries": [{"text": "cue", "context": "a", "expected": "alpha"}, {"text": "cue", "context": "b", "expected": "beta"}]},
        {"id": "recurrent", "training": [{"symbols": [value]} for _ in range(10) for value in ("a", "x", "alpha", "b", "x", "beta")],
         "queries": [{"text": "a x", "expected": "alpha"}, {"text": "b x", "expected": "beta"}]},
        {"id": "hierarchy", "training": [{"symbols": ["project/python/install"]}] * 10,
         "queries": [{"text": "project/python", "expected": "project/python/install"}]},
    ]


def evaluate(cases: list[JsonObject]) -> list[JsonValue]:
    from codey.types.json_value import decode_json_object, require_json_string

    results: list[JsonValue] = []
    for mechanism in MECHANISMS:
        for case in cases:
            model = ResearchMemory(mechanism)
            training = case["training"]
            if not isinstance(training, list):
                raise ValueError("training must be an array")
            started = time.perf_counter()
            events: list[MemoryEvent] = []
            for index, value in enumerate(training):
                event = MemoryEvent(str(index), "research.observed", AT, "", "", "", "project:fixture",
                                    decode_json_object(value), ("synthetic:" + str(case["id"]),))
                events.append(event)
                model.observe(event)
            frozen = model.snapshot()
            restored = ResearchMemory(mechanism)
            restored.restore(frozen)
            if restored.snapshot() != frozen:
                raise ValueError("mechanism snapshot changed state")
            replayed = ResearchMemory(mechanism)
            for event in events:
                replayed.observe(event)
            if replayed.snapshot() != frozen:
                raise ValueError("mechanism replay changed state")
            queries = case["queries"]
            if not isinstance(queries, list):
                raise ValueError("queries must be an array")
            passed = 0
            for value in queries:
                query = decode_json_object(value)
                answer = model.retrieve(MemoryQuery(require_json_string(query["text"], "text"), ("project:fixture",), AT,
                    max_items=1, context=require_json_string(query.get("context", ""), "context")))
                passed += int(bool(answer.items) and answer.items[0].text == query["expected"])
            results.append({"mechanism": mechanism, "case": case["id"], "passed": passed, "queries": len(queries),
                "nodes": len(model.counts), "edges": len(model.weights), "updates": model.updates,
                "operations_proxy": model.operations, "snapshot_bytes": len(canonical(frozen).encode()),
                "seconds": time.perf_counter() - started, "snapshot_and_replay": True,
                "production_enabled": False, "model_quality_claim": False})
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args(namespace=Arguments())
    args.evidence.mkdir(parents=True, exist_ok=False)
    cases = corpus()
    case_values: list[JsonValue] = list(cases)
    inputs: JsonObject = {"schema_version": 1, "cases": case_values,
        "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                          (Path(__file__), Path("tools/memory_plasticity_mechanisms.py"), Path("codey/memory/contracts.py"))}}
    (args.evidence / "protocol.json").write_text(canonical(inputs) + "\n", encoding="utf-8")
    results: JsonObject = {"protocol_sha256": hashlib.sha256(canonical(inputs).encode()).hexdigest(), "results": evaluate(cases),
                          "limitation": "small synthetic mechanism probes; no production or model-quality promotion"}
    (args.evidence / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(MECHANISMS)} mechanisms on {len(cases)} frozen synthetic cases")


if __name__ == "__main__":
    main()
