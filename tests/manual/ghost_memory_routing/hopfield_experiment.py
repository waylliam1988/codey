"""Reproducible isolated Hopfield pilot. No production writes, model loading or retries.

Freeze: --output DIR. Offline: --output DIR --offline.
Live: --output DIR --live --endpoint http://127.0.0.1:5001/v1.
Exact duplicate requests share a single observed response (reported explicitly).
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
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import cast

import numpy as np
import psutil

from tests.manual.ghost_memory_routing.hopfield_methods import Index
from tests.manual.ghost_memory_routing.hopfield_protocol import (
    ACTIONS,
    ARMS,
    CONFIG,
    Gold,
    Query,
    eligible,
    fixture,
    fixture_json,
    prompt,
    render,
    score_action,
)

ROOT = Path(__file__).resolve().parents[3]
SOURCES = (
    Path(__file__), Path(__file__).with_name("hopfield_protocol.py"),
    Path(__file__).with_name("hopfield_methods.py"), ROOT / "tests/test_ghost_hopfield_experiment.py",
    ROOT / "docs/ghost_progress/hopfield-memory-protocol-20261118.md",
)


def dump(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity() -> dict[str, object]:
    return {
        "config": CONFIG, "fixture_sha256": digest(fixture_json().encode()),
        "sources": {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in SOURCES},
        "gate": {
            "minimum_net_extra_correct_vs_cosine": 4, "no_family_regression": True,
            "zero_lifecycle_exposures": True, "must_beat_latest_rule": True,
            "meaning": "pilot signal only; never production approval",
        },
    }


def freeze(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    dump(home / "freeze.json", identity())
    (home / "fixture.json").write_text(fixture_json(), encoding="utf-8")
    dump(home / "environment.json", {
        "python": platform.python_version(), "platform": platform.platform(), "numpy": np.__version__,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
    })


def check_freeze(home: Path) -> None:
    if json.loads((home / "freeze.json").read_text(encoding="utf-8")) != identity():
        raise ValueError("source/fixture/config changed after freeze; do not overwrite or silently resume")


def cell(query: Query, gold: Gold, arm: str) -> dict[str, object]:
    start = time.perf_counter_ns()
    index = Index(query, arm)
    built = time.perf_counter_ns()
    context, selected = render(index.ranked(query))
    finished = time.perf_counter_ns()
    relevant = set(gold.relevant)
    selected_ids = tuple(m.id for m in selected)
    selected_set = set(selected_ids)
    valid_ids = {m.id for m in eligible(query)}
    top = selected[0].action if selected else "ask_user"
    return {
        "query": query.id, "profile": query.profile, "family": query.family, "updates": query.updates,
        "arm": arm, "selected": selected_ids, "context": context,
        "context_chars": len(context), "array_bytes": index.array_bytes,
        "build_ms": (built - start) / 1e6, "query_render_ms": (finished - built) / 1e6,
        "top1": bool(selected_ids and selected_ids[0] in relevant) if relevant else not selected_ids,
        "recall_at_2": len(relevant & selected_set) / len(relevant) if relevant else None,
        "hit_at_2": bool(relevant & selected_set) if relevant else None,
        "selection_action_correct": top == gold.action,
        "conflicting_values": sum(m.action != gold.action for m in selected) if relevant else 0,
        "ineligible_exposures": len(selected_set - valid_ids),
        "expected": gold.action,
    }


def percentile(values: list[float], q: float) -> float:
    return round(float(np.percentile(values, q)), 4) if values else 0.0


def summarize(rows: list[dict[str, object]], *, live: bool) -> dict[str, object]:
    output: dict[str, object] = {}
    for arm in ARMS:
        subset = [r for r in rows if r["arm"] == arm]
        recall = [float(cast(float, r["recall_at_2"])) for r in subset if r["recall_at_2"] is not None]
        entry: dict[str, object] = {
            "n": len(subset), "top1_correct": sum(bool(r["top1"]) for r in subset),
            "mean_recall_at_2": round(statistics.mean(recall), 4) if recall else None,
            "relevant_queries": len(recall),
            "hit_at_2_correct": sum(bool(r["hit_at_2"]) for r in subset if r["hit_at_2"] is not None),
            "selection_action_correct": sum(bool(r["selection_action_correct"]) for r in subset),
            "conflicting_values": sum(int(cast(int, r["conflicting_values"])) for r in subset),
            "ineligible_exposures": sum(int(cast(int, r["ineligible_exposures"])) for r in subset),
            "context_chars_mean": round(statistics.mean(int(cast(int, r["context_chars"])) for r in subset), 2),
            "context_chars_max": max((int(cast(int, r["context_chars"])) for r in subset), default=0),
        }
        if live:
            entry["behavior_correct"] = sum(bool(r["behavior_correct"]) for r in subset)
            entry["invalid_actions"] = sum(r["action"] is None for r in subset)
            entry["by_family"] = {
                family: {"n": sum(r["family"] == family for r in subset),
                         "correct": sum(bool(r["behavior_correct"]) for r in subset if r["family"] == family)}
                for family in ("cue", "context", "drift", "lifecycle")
            }
            fresh = [r for r in subset if not r["reused"]]
            entry["physical_requests"] = len(fresh)
            for field in ("model_ms", "prompt_tokens", "completion_tokens"):
                values = [float(cast(float, r[field])) for r in fresh if r[field] is not None]
                entry[field + "_p50"] = percentile(values, 50)
                entry[field + "_p95"] = percentile(values, 95)
        output[arm] = entry
    metric = "behavior_correct" if live else "selection_action_correct"
    paired: dict[str, object] = {}
    for baseline in ARMS[:-1]:
        baseline_rows = {str(r["query"]): r for r in rows if r["arm"] == baseline}
        pairs = [(r, baseline_rows[str(r["query"])]) for r in rows
                 if r["arm"] == "hopfield" and str(r["query"]) in baseline_rows]
        paired[baseline] = {
            "n": len(pairs), "wins": sum(bool(a[metric]) and not b[metric] for a, b in pairs),
            "losses": sum(bool(b[metric]) and not a[metric] for a, b in pairs),
            "same_selection": sum(a["selected"] == b["selected"] for a, b in pairs),
        }
    output["hopfield_pairs"] = paired
    output["adaptation"] = adaptation(rows, metric)
    return output


def adaptation(rows: list[dict[str, object]], metric: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for arm in ARMS:
        subset = [r for r in rows if r["arm"] == arm and r["family"] == "drift"]
        curves = {str(step): sum(bool(r[metric]) for r in subset if r["updates"] == step) for step in range(5)}
        sustained: list[int | None] = []
        for profile in range(int(CONFIG["profiles"])):
            by_step = {int(cast(int, r["updates"])): bool(r[metric]) for r in subset if r["profile"] == profile}
            sustained.append(next((step for step in range(1, 5)
                                   if all(by_step.get(later, False) for later in range(step, 5))), None))
        result[arm] = {"correct_by_update": curves, "profiles_per_step": int(CONFIG["profiles"]),
                       "updates_to_sustained_correct_by_profile": sustained}
    return result


def benchmark() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    base = fixture()[0][0]
    process = psutil.Process()
    for size in CONFIG["benchmark_sizes"]:
        # Repetition measures scaling of this annotated workload, not capacity/accuracy at scale.
        expanded = tuple(replace(base.memories[i % len(base.memories)], id=f"scale-{i}") for i in range(size))
        query = replace(base, memories=expanded)
        for arm in ARMS:
            tracemalloc.start()
            rss_before = process.memory_info().rss
            start = time.perf_counter_ns()
            index = Index(query, arm)
            build_ms = (time.perf_counter_ns() - start) / 1e6
            rss_after = process.memory_info().rss
            _, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            index.ranked(query)  # warm-up is not included
            durations: list[float] = []
            for _ in range(int(CONFIG["benchmark_queries"])):
                start = time.perf_counter_ns()
                index.ranked(query)
                durations.append((time.perf_counter_ns() - start) / 1e6)
            result.append({
                "size": size, "arm": arm, "build_ms_instrumented": round(build_ms, 4),
                "query_ms_p50": percentile(durations, 50), "query_ms_p95": percentile(durations, 95),
                "array_bytes": index.array_bytes, "traced_build_peak_bytes": traced_peak,
                "rss_before_bytes": rss_before, "rss_after_bytes": rss_after,
            })
    return result


def offline(home: Path) -> None:
    check_freeze(home)
    if (home / "offline.json").exists():
        raise ValueError("offline result already exists")
    rows = [cell(query, gold, arm) for query, gold in fixture() for arm in ARMS]
    dump(home / "offline.json", {"status": "COMPLETE", "cells": rows, "summary": summarize(rows, live=False),
                                  "benchmark": benchmark()})


def request_body(query: Query, context: str, model: str) -> bytes:
    schema = {"type": "object", "properties": {"action": {"type": "string", "enum": list(ACTIONS)}},
              "required": ["action"], "additionalProperties": False}
    return json.dumps({
        "model": model, "messages": prompt(query, context), "temperature": CONFIG["temperature"],
        "max_tokens": CONFIG["max_tokens"], "stream": False,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "ghost_memory_action", "strict": True, "schema": schema,
        }},
    }, ensure_ascii=False, sort_keys=True).encode()


def models(endpoint: str) -> tuple[bytes, str]:
    with urllib.request.urlopen(endpoint + "/models", timeout=10) as response:
        raw = response.read()
    body = json.loads(raw)
    return raw, str(body["data"][0]["id"])


def generate(endpoint: str, payload: bytes, directory: Path) -> dict[str, object]:
    directory.mkdir()
    (directory / "request.json").write_bytes(payload)
    start = time.perf_counter_ns()
    request = urllib.request.Request(endpoint + "/chat/completions", data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=int(CONFIG["timeout_seconds"])) as response:
            raw = response.read()
            status = response.status
        elapsed = (time.perf_counter_ns() - start) / 1e6
        (directory / "response.json").write_bytes(raw)
        parsed = json.loads(raw)
        choice = parsed["choices"][0]
        if choice["finish_reason"] != "stop":
            raise ValueError("incomplete response: " + str(choice["finish_reason"]))
        reply = choice["message"]["content"]
        (directory / "reply.txt").write_text(reply, encoding="utf-8")
        usage = parsed.get("usage", {})
        metrics: dict[str, object] = {
            "action": score_action(reply), "model_ms": elapsed, "http_status": status,
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "response_sha256": digest(raw), "response_model": parsed.get("model"),
        }
        dump(directory / "metrics.json", metrics)
        return metrics
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            (directory / "http-error.bin").write_bytes(exc.read())
        dump(directory / "failure.json", {"error": repr(exc), "elapsed_ms": (time.perf_counter_ns() - start) / 1e6})
        raise


def evaluate_gate(rows: list[dict[str, object]]) -> dict[str, object]:
    by_arm = {arm: [r for r in rows if r["arm"] == arm] for arm in ARMS}
    counts = {arm: sum(bool(r["behavior_correct"]) for r in group) for arm, group in by_arm.items()}
    no_regression = all(
        sum(bool(r["behavior_correct"]) for r in by_arm["hopfield"] if r["family"] == family)
        >= sum(bool(r["behavior_correct"]) for r in by_arm["cosine"] if r["family"] == family)
        for family in ("cue", "context", "drift", "lifecycle")
    )
    checks = {
        "complete_matrix": len(rows) == len(fixture()) * len(ARMS),
        "net_at_least_four_over_cosine": counts["hopfield"] - counts["cosine"] >= 4,
        "no_family_regression_vs_cosine": no_regression,
        "beats_latest_rule": counts["hopfield"] > counts["latest_rule"],
        "zero_ineligible_exposures": all(r["ineligible_exposures"] == 0 for r in rows),
    }
    return {"checks": checks, "passes": all(checks.values()), "production": "EXPERIMENT_ONLY"}


def live(home: Path, endpoint: str) -> None:
    check_freeze(home)
    live_home = home / "live"
    live_home.mkdir()  # deliberately no resume or retries
    rows: list[dict[str, object]] = []
    cache: dict[str, dict[str, object]] = {}
    current_cell = "models-before"
    physical = 0
    try:
        raw_models, model = models(endpoint)
        (live_home / "models-before.json").write_bytes(raw_models)
        with (live_home / "cells.jsonl").open("x", encoding="utf-8", buffering=1) as stream:
            for number, (query, gold) in enumerate(fixture()):
                rotation = number % len(ARMS)
                for arm in ARMS[rotation:] + ARMS[:rotation]:
                    current_cell = query.id + "/" + arm
                    row = cell(query, gold, arm)
                    payload = request_body(query, str(row.pop("context")), model)
                    key = digest(payload)
                    reused = key in cache
                    if not reused:
                        metrics = generate(endpoint, payload, live_home / f"request-{physical:04d}-{key[:10]}")
                        physical += 1
                        cache[key] = {**metrics, "physical_request": physical - 1}
                        if physical == 1 and metrics["action"] is None:
                            raise ValueError("first reply violates frozen structured-response contract")
                    row.update(cache[key])
                    row.update({"request_sha256": key, "reused": reused,
                                "behavior_correct": row["action"] == gold.action})
                    rows.append(row)
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(json.dumps({"cell": current_cell, "physical_requests": physical,
                                      "completed_cells": len(rows), "reused": reused,
                                      "correct": row["behavior_correct"]}), flush=True)
        current_cell = "models-after"
        after, after_model = models(endpoint)
        (live_home / "models-after.json").write_bytes(after)
        if model != after_model:
            raise ValueError("model identity changed during experiment")
        dump(live_home / "result.json", {
            "status": "COMPLETE", "model": model, "logical_cells": len(rows), "physical_requests": physical,
            "ghost_extra_generation_requests": 0, "exact_request_reuse": len(rows) - physical,
            "physical_latency_ms_p50": percentile([float(cast(float, m["model_ms"])) for m in cache.values()], 50),
            "physical_latency_ms_p95": percentile([float(cast(float, m["model_ms"])) for m in cache.values()], 95),
            "physical_prompt_tokens": sum(int(cast(int, m["prompt_tokens"]) or 0) for m in cache.values()),
            "physical_completion_tokens": sum(int(cast(int, m["completion_tokens"]) or 0) for m in cache.values()),
            "summary": summarize(rows, live=True), "gate": evaluate_gate(rows),
        })
    except Exception as exc:
        dump(live_home / "failure.json", {
            "status": "INCOMPLETE", "cell": current_cell, "error": repr(exc),
            "completed_cells": len(rows), "successful_physical_requests": physical, "retries": 0,
            "summary": summarize(rows, live=True) if rows else {},
        })
        raise


def report(home: Path, target: Path) -> None:
    check_freeze(home)
    offline_data = json.loads((home / "offline.json").read_text(encoding="utf-8"))
    live_path = home / "live/result.json"
    live_data = json.loads(live_path.read_text(encoding="utf-8")) if live_path.exists() else None
    failure = home / "live/failure.json"
    payload = {
        "experiment": "ghost-hopfield-annotated-pilot-v1", "freeze": identity(),
        "status": "COMPLETE" if live_data else "INCOMPLETE", "production": "EXPERIMENT_ONLY",
        "queries": len(fixture()), "arms": ARMS, "offline_summary": offline_data["summary"],
        "benchmark": offline_data["benchmark"], "live": live_data,
        "failure": json.loads(failure.read_text(encoding="utf-8")) if failure.exists() else None,
        "limitations": [
            "Synthetic, preannotated representation; no automatic semantic encoder evaluated.",
            "Four synthetic profiles are clustered, not 44 independent users.",
            "Exact duplicate wire requests share one model observation; no stochastic replication.",
            "Fixed state means frozen per-context calibration exemplars, selected with cosine+recency.",
            "Hebbian arm is a simple linear outer-product comparator, not the production association graph.",
            "Lexical arm reuses production scoring at frozen time, not its complete retrieval/admission runtime.",
            "Index rebuilding after common lifecycle filtering is not neural unlearning.",
            "Scale benchmark repeats keys; it measures cost, not memory capacity at TB scale.",
            "RSS includes interpreter/allocator; traced peak and array bytes are separate measurements.",
            "Direct local chat-completions transport; not production runtime/builder/admission acceptance.",
        ],
    }
    dump(target, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--report", type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001/v1")
    options = parser.parse_args()
    if options.offline:
        offline(options.output)
    elif options.live:
        live(options.output, options.endpoint.rstrip("/"))
    elif options.report:
        report(options.output, options.report)
    else:
        freeze(options.output)


if __name__ == "__main__":
    main()
