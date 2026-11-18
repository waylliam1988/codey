"""Preregistered development/held-out study. No production changes or model calls.

CLI phases: freeze -> develop -> test. Live is a separate frozen consumer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import time
import tracemalloc
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

import numpy as np
import psutil

from tests.manual.ghost_memory_routing.hopfield_v2_data import (
    ARMS,
    BETAS,
    BOOTSTRAPS,
    BUDGET,
    DEV_STREAMS,
    DIMENSION,
    ENCODINGS,
    LIMIT,
    MIN_EFFECT,
    SEEDS,
    STEPS,
    TEST_STREAMS,
    THRESHOLDS,
    Encoder,
    Event,
    Gold,
    Parameters,
    Query,
    dataset,
    eligible,
    observable_candidates,
    render,
    serializable,
    stream,
)
from tests.manual.ghost_memory_routing.hopfield_v2_methods import GhostCache, Prepared, deletion_probe, select

ROOT = Path(__file__).resolve().parents[3]
FILES = (
    "tests/manual/ghost_memory_routing/hopfield_v2_data.py",
    "tests/manual/ghost_memory_routing/hopfield_v2_methods.py",
    "tests/manual/ghost_memory_routing/hopfield_v2_study.py",
    "tests/manual/ghost_memory_routing/hopfield_v2_live.py",
    "tests/test_ghost_hopfield_v2.py",
    "docs/ghost_progress/hopfield-confirmatory-protocol-20261118.md",
)
LIVE_CASES = ("partial", "noisy", "context-a", "context-b", "drift-1", "drift-4", "expiry", "deletion")
LIVE_STREAMS = (2, 9, 14, 23, 31, 38, 46, 55, 63, 71, 82, 91)


@dataclass(frozen=True)
class Selection:
    threshold: float
    parameters: dict[str, Parameters]
    primary_baseline: str


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode()


def dump(path: Path, value: object) -> None:
    with path.open("xb") as output:
        output.write(json_bytes(value))


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def production_hashes() -> dict[str, str]:
    return {str(path.relative_to(ROOT)): sha(path.read_bytes()) for path in (ROOT / "codey").rglob("*.py")}


def identity() -> dict[str, object]:
    return {
        "version": "ghost-hopfield-v2", "files": {name: sha((ROOT / name).read_bytes()) for name in FILES},
        "fixture_hashes": {split: sha(json_bytes(serializable(split))) for split in ("dev", "test")},
        "arms": ARMS, "encodings": ENCODINGS, "seeds": SEEDS, "dev_streams": DEV_STREAMS,
        "test_streams": TEST_STREAMS, "betas": BETAS, "steps": STEPS, "thresholds": THRESHOLDS,
        "dimension": DIMENSION, "budget_chars": BUDGET, "max_memories": LIMIT,
        "live_cases": LIVE_CASES, "live_streams": LIVE_STREAMS, "live_seed": SEEDS[-1],
        "live_max_tokens": 96, "live_timeout_seconds": 120, "live_temperature": 0,
        "bootstrap_repetitions": BOOTSTRAPS, "primary_interval": 0.9875, "minimum_effect": MIN_EFFECT,
    }


def freeze(home: Path) -> None:
    home.mkdir(parents=True)
    dump(home / "freeze.json", identity())
    dump(home / "production-before.json", production_hashes())
    dump(home / "environment.json", {
        "python": platform.python_version(), "numpy": np.__version__, "platform": platform.platform(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
    })


def check(home: Path) -> None:
    if json.loads((home / "freeze.json").read_bytes()) != json.loads(json_bytes(identity())):
        raise ValueError("frozen sources/fixture/config changed")
    if json.loads((home / "production-before.json").read_bytes()) != production_hashes():
        raise ValueError("production Python tree changed during experiment")


def grid(arm: str) -> tuple[Parameters, ...]:
    if arm in ("lexical", "latest", "ghost_adapter"):
        return (Parameters(),)
    steps = STEPS if arm in ("linear", "hopfield", "anchored") else (1,)
    return tuple(Parameters(beta, step) for beta in BETAS for step in steps)


def metrics(query: Query, gold: Gold, selected: tuple[Event, ...]) -> dict[str, object]:
    relevant = set(gold.relevant)
    identifiers = {event.id for event in selected}
    success = bool(identifiers & relevant) if relevant else not selected
    precision = len(identifiers & relevant) / len(selected) if selected else float(not relevant)
    return {
        "hit": success, "top1": bool(selected and selected[0].id in relevant) if relevant else not selected,
        "precision_at_2": precision,
        "recall_at_2": len(identifiers & relevant) / len(relevant) if relevant else None,
        "ineligible_exposures": len(identifiers - {event.id for event in eligible(query)}),
        "wrong_context": sum(event.context != gold.context for event in selected),
    }


def calibrate(dev: tuple[tuple[Query, Gold], ...], encoders: list[Encoder]) -> tuple[float, list[dict[str, object]]]:
    totals = {threshold: np.zeros(4, dtype=np.float64) for threshold in THRESHOLDS}
    for encoder in encoders:
        for query, gold in dev:
            q = encoder.vector(query.text, query.facets)
            for event in observable_candidates(query):
                similarity = float(encoder.vector(event.text, event.facets, event.action) @ q)
                applicable = event.context == gold.context and bool(gold.relevant)
                for threshold in THRESHOLDS:
                    accepted = similarity >= threshold
                    totals[threshold][(0 if accepted else 1) if applicable else (2 if accepted else 3)] += 1
    scores: list[dict[str, object]] = []
    for threshold, total in totals.items():
        tp, fn, fp, tn = (float(value) for value in total)
        balanced = (tp / max(1, tp + fn) + tn / max(1, tn + fp)) / 2
        scores.append({"threshold": threshold, "balanced_accuracy": float(balanced),
                       "tp": int(tp), "fn": int(fn), "fp": int(fp), "tn": int(tn)})
    best = max(scores, key=lambda row: (cast(float, row["balanced_accuracy"]), -cast(float, row["threshold"])))
    return cast(float, best["threshold"]), scores


def develop(home: Path) -> None:
    check(home)
    if (home / "development.json").exists():
        raise ValueError("refusing to overwrite development selection")
    dev = dataset("dev")
    cache = GhostCache()
    results: dict[str, object] = {}
    for encoding in ENCODINGS:
        encoders = [Encoder(encoding, seed, dev) for seed in SEEDS]
        threshold, gate_scores = calibrate(dev, encoders)
        totals = {(arm, parameters): np.zeros(4, dtype=np.float64) for arm in ARMS for parameters in grid(arm)}
        for encoder in encoders:
            for position, (query, gold) in enumerate(dev):
                prepared = Prepared(query, encoder, threshold)
                for (arm, parameters), total in totals.items():
                    selected = select(prepared, arm, parameters, cache)
                    row = metrics(query, gold, selected)
                    total += (float(bool(row["hit"])), cast(float, row["precision_at_2"]),
                              float(bool(row["top1"])), 1.0)
                if position % 112 == 0:
                    print(f"development {encoding} seed={encoder.seed} query={position}/{len(dev)}", flush=True)
        score_rows: list[dict[str, object]] = []
        chosen: dict[str, Parameters] = {}
        for arm in ARMS:
            options = []
            for parameters in grid(arm):
                hit, precision, top1, n = totals[arm, parameters]
                score_row: dict[str, object] = {
                    "arm": arm, "parameters": asdict(parameters), "hit": float(hit / n),
                    "precision": float(precision / n), "top1": float(top1 / n), "n": int(n),
                }
                score_rows.append(score_row)
                options.append((float(hit / n), float(precision / n), float(top1 / n),
                                -parameters.steps, -parameters.beta, parameters))
            chosen[arm] = max(options, key=lambda option: option[:-1])[-1]
        simple = ("cosine", "linear", "latest")
        baseline = max(simple, key=lambda arm: tuple(totals[arm, chosen[arm]][:3]))
        results[encoding] = {"threshold": threshold, "gate_scores": gate_scores,
                             "parameters": {arm: asdict(parameters) for arm, parameters in chosen.items()},
                             "primary_baseline": baseline, "grid": score_rows}
    dump(home / "development.json", {"selection": results, "ghost_store_builds": cache.builds,
                                      "test_data_used": False})
    dump(home / "development-lock.json", {"sha256": sha((home / "development.json").read_bytes())})


def selections(home: Path) -> dict[str, Selection]:
    lock = json.loads((home / "development-lock.json").read_bytes())["sha256"]
    if sha((home / "development.json").read_bytes()) != lock:
        raise ValueError("development selection changed after lock")
    content = json.loads((home / "development.json").read_bytes())["selection"]
    return {encoding: Selection(float(value["threshold"]), {
        arm: Parameters(float(parameters["beta"]), int(parameters["steps"]))
        for arm, parameters in value["parameters"].items()
    }, str(value["primary_baseline"])) for encoding, value in content.items()}


def bootstrap(deltas: list[float], *, interval: float = 0.9875) -> dict[str, object]:
    values = np.array(deltas, dtype=np.float64)
    rng = np.random.default_rng(20261118)
    samples = values[rng.integers(0, len(values), size=(BOOTSTRAPS, len(values)))].mean(axis=1)
    tail = (1 - interval) / 2
    low, high = np.quantile(samples, [tail, 1 - tail])
    mean = float(values.mean())
    return {"clusters": len(deltas), "mean_delta": mean, "interval_level": interval,
            "lower": float(low), "upper": float(high),
            "interpretation": "POSITIVE_SIGNAL" if mean >= MIN_EFFECT and low > 0 else
                ("EXCLUDES_FIVE_POINT_GAIN_ON_THIS_GENERATOR" if high < MIN_EFFECT else "INCONCLUSIVE")}


def summarize(rows: list[dict[str, object]], selected: dict[str, Selection],
              *, endpoint: str = "hit") -> dict[str, object]:
    output: dict[str, object] = {}
    for encoding in ENCODINGS:
        subset = [row for row in rows if row["encoding"] == encoding]
        arms = tuple(dict.fromkeys(str(row["arm"]) for row in subset))
        summary: dict[str, object] = {}
        for arm in arms:
            group = [row for row in subset if row["arm"] == arm]
            summary[arm] = {
                "n": len(group), "correct": sum(bool(row[endpoint]) for row in group),
                "rate": statistics.mean(float(bool(row[endpoint])) for row in group),
                "families": {family: {
                    "n": sum(row["family"] == family for row in group),
                    "rate": statistics.mean(float(bool(row[endpoint])) for row in group if row["family"] == family),
                } for family in ("cue", "context", "drift", "lifecycle")},
                "exposures": sum(int(cast(int, row["ineligible_exposures"])) for row in group),
                "wrong_context": sum(int(cast(int, row["wrong_context"])) for row in group),
                "precision_at_2": statistics.mean(cast(float, row["precision_at_2"]) for row in group),
            }
        comparisons: list[dict[str, object]] = []
        for candidate_arm in ("hopfield", "anchored"):
            for baseline in dict.fromkeys((selected[encoding].primary_baseline, "cosine", "ghost_adapter")):
                by_key = {(str(row["query"]), int(cast(int, row["seed"])), str(row["arm"])): row
                          for row in subset if row["arm"] in (baseline, candidate_arm)}
                per_stream: dict[str, list[float]] = {}
                for row in subset:
                    if row["arm"] != candidate_arm:
                        continue
                    other = by_key[str(row["query"]), int(cast(int, row["seed"])), baseline]
                    per_stream.setdefault(str(row["stream"]), []).append(
                        float(bool(row[endpoint])) - float(bool(other[endpoint])))
                uncertainty = bootstrap([statistics.mean(values) for _, values in sorted(per_stream.items())])
                candidate_summary = cast(dict[str, object], summary[candidate_arm])
                baseline_summary = cast(dict[str, object], summary[baseline])
                candidate_families = cast(dict[str, dict[str, float]], candidate_summary["families"])
                baseline_families = cast(dict[str, dict[str, float]], baseline_summary["families"])
                no_harm = all(candidate_families[family]["rate"] - baseline_families[family]["rate"] >= -0.05
                              for family in candidate_families)
                safe = candidate_summary["exposures"] == 0
                comparisons.append({
                    "candidate": candidate_arm, "baseline": baseline,
                    "preregistered_primary": baseline == selected[encoding].primary_baseline,
                    **uncertainty, "no_family_regression_over_five_points": no_harm,
                    "zero_exposures": safe,
                    "qualified_positive": uncertainty["interpretation"] == "POSITIVE_SIGNAL" and no_harm and safe,
                })
        output[encoding] = {"arms": summary, "comparisons": comparisons,
                            "development_selected_baseline": selected[encoding].primary_baseline}
    return output


def adaptation(rows: list[dict[str, object]], endpoint: str = "hit") -> dict[str, object]:
    result: dict[str, object] = {}
    for encoding in ENCODINGS:
        arms: dict[str, object] = {}
        for arm in ARMS:
            group = [row for row in rows if row["encoding"] == encoding and row["arm"] == arm
                     and row["family"] == "drift"]
            curves = {str(step): statistics.mean(float(bool(row[endpoint])) for row in group if row["updates"] == step)
                      for step in range(5)}
            per_stream_seed: dict[tuple[str, int], dict[int, bool]] = {}
            for row in group:
                per_stream_seed.setdefault((str(row["stream"]), int(cast(int, row["seed"]))), {})[
                    int(cast(int, row["updates"]))] = bool(row[endpoint])
            sustained = [next((step for step in range(1, 5) if all(values[later] for later in range(step, 5))), None)
                         for _, values in sorted(per_stream_seed.items())]
            arms[arm] = {"correct_by_update": curves, "stream_seed_pairs": len(sustained),
                         "sustained_after_first_correction": sum(step == 1 for step in sustained),
                         "not_sustained_by_four": sum(step is None for step in sustained)}
        result[encoding] = arms
    return result


def test(home: Path) -> None:
    check(home)
    selected = selections(home)
    if (home / "heldout-cells.jsonl").exists():
        raise ValueError("refusing to overwrite held-out observations")
    dev, heldout = dataset("dev"), dataset("test")
    cache = GhostCache()
    rows: list[dict[str, object]] = []
    with (home / "heldout-cells.jsonl").open("x", encoding="utf-8", buffering=1) as output:
        for encoding in ENCODINGS:
            for seed in SEEDS:
                encoder = Encoder(encoding, seed, dev)
                for position, (query, gold) in enumerate(heldout):
                    prepared = Prepared(query, encoder, selected[encoding].threshold)
                    for arm in ARMS:
                        events = select(prepared, arm, selected[encoding].parameters[arm], cache)
                        row = {
                            "query": query.id, "stream": query.stream, "family": query.family,
                            "updates": query.updates, "encoding": encoding, "seed": seed, "arm": arm,
                            "selected": tuple(event.id for event in events),
                            "context_chars": len(render(events)[0]), **metrics(query, gold, events),
                        }
                        rows.append(row)
                        output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    if position % 112 == 0:
                        print(f"heldout {encoding} seed={seed} query={position}/{len(heldout)}", flush=True)
    dump(home / "heldout-result.json", {
        "status": "COMPLETE", "logical_cells": len(rows), "independent_streams": TEST_STREAMS,
        "summary": summarize(rows, selected), "adaptation": adaptation(rows),
        "ghost_store_builds_with_reopen_replay": cache.builds,
        "deletion_probe": deletion_probe(home / "deletion-probe"), "production_unchanged": True,
    })


def benchmark(home: Path) -> None:
    check(home)
    chosen = selections(home)
    dev = dataset("dev")
    base = stream("scale", 0)[0][0]
    events: list[Event] = []
    for number in range(512):
        query = stream("scale", number)[0][0]
        events.extend(replace(event, project=base.project, owner="self", expires=None, deleted=False)
                      for event in query.events)
        if len(events) >= 8192:
            break
    rows: list[dict[str, object]] = []
    for size in (64, 512, 2048, 8192):
        query = replace(base, visible_context="", events=tuple(events[:size]))
        for arm in ("cosine", "linear", "hopfield", "anchored"):
            encoder = Encoder("concept", SEEDS[-1], dev)
            tracemalloc.start()
            start = time.perf_counter_ns()
            prepared = Prepared(query, encoder, 0.0)
            if arm == "linear":
                prepared.weights = prepared.keys.T @ prepared.keys / len(prepared.events)
            build_ms = (time.perf_counter_ns() - start) / 1e6
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            array_bytes = prepared.keys.nbytes + prepared.vector.nbytes + prepared.prior.nbytes
            if prepared.weights is not None:
                array_bytes += prepared.weights.nbytes
            cache = GhostCache()
            select(prepared, arm, chosen["concept"].parameters[arm], cache)
            times: list[float] = []
            for _ in range(25):
                start = time.perf_counter_ns()
                select(prepared, arm, chosen["concept"].parameters[arm], cache)
                times.append((time.perf_counter_ns() - start) / 1e6)
            rows.append({"size": len(prepared.events), "arm": arm, "build_ms_instrumented": build_ms,
                         "query_ms_p50": float(np.percentile(times, 50)),
                         "query_ms_p95": float(np.percentile(times, 95)), "array_bytes": array_bytes,
                         "encoder_cache_array_bytes": sum(value.nbytes for value in encoder._cache.values()),
                         "traced_build_peak": peak, "process_rss": psutil.Process().memory_info().rss})
    dump(home / "benchmark.json", rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("freeze", "develop", "test", "benchmark"))
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    {"freeze": freeze, "develop": develop, "test": test, "benchmark": benchmark}[options.phase](options.output)


if __name__ == "__main__":
    main()
