"""Supplementary cost check with exactly the advertised number of candidate keys.

The frozen benchmark's threshold=0 filtered negative similarities, so its
actual sizes were 46/296/1108/4726. Preserve it. This supplement disables only
that gate for cost measurement; no quality score or selected model is changed.
Run after live to avoid competing with local inference for timing resources.
"""

from __future__ import annotations

import argparse
import time
import tracemalloc
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import psutil

from tests.manual.ghost_memory_routing.hopfield_v2_data import SEEDS, Encoder, Event, dataset, stream
from tests.manual.ghost_memory_routing.hopfield_v2_methods import GhostCache, Prepared, select
from tests.manual.ghost_memory_routing.hopfield_v2_study import check, dump, selections


def run(home: Path, target: Path) -> None:
    check(home)
    if not (home / "live/result.json").exists():
        raise ValueError("complete live before isolated timing supplement")
    chosen = selections(home)["concept"]
    dev = dataset("dev")
    base = stream("scale", 0)[0][0]
    pool: list[Event] = []
    for number in range(512):
        query = stream("scale", number)[0][0]
        pool.extend(replace(event, project=base.project, owner="self", expires=None, deleted=False)
                    for event in query.events)
        if len(pool) >= 8192:
            break
    rows: list[dict[str, object]] = []
    for size in (64, 512, 2048, 8192):
        query = replace(base, visible_context="", events=tuple(pool[:size]))
        assert len({event.id for event in query.events}) == size
        for arm in ("cosine", "linear", "hopfield", "anchored"):
            encoder = Encoder("concept", SEEDS[-1], dev)
            tracemalloc.start()
            before_rss = psutil.Process().memory_info().rss
            start = time.perf_counter_ns()
            prepared = Prepared(query, encoder, -float("inf"))
            assert len(prepared.events) == size
            if arm == "linear":
                prepared.weights = prepared.keys.T @ prepared.keys / size
            built = time.perf_counter_ns()
            build_ms = (built - start) / 1e6
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            arrays = prepared.keys.nbytes + prepared.vector.nbytes + prepared.prior.nbytes
            if prepared.weights is not None:
                arrays += prepared.weights.nbytes
            cache = GhostCache()
            select(prepared, arm, chosen.parameters[arm], cache)
            times: list[float] = []
            for _ in range(25):
                start = time.perf_counter_ns()
                select(prepared, arm, chosen.parameters[arm], cache)
                times.append((time.perf_counter_ns() - start) / 1e6)
            rows.append({
                "requested_size": size, "actual_size": len(prepared.events), "arm": arm,
                "parameters": asdict(chosen.parameters[arm]),
                "build_ms_instrumented": build_ms,
                "query_ms_p50": float(np.percentile(times, 50)),
                "query_ms_p95": float(np.percentile(times, 95)),
                "index_array_bytes": arrays,
                "encoder_cache_array_bytes": sum(value.nbytes for value in encoder._cache.values()),
                "traced_build_peak_bytes": peak, "rss_before_bytes": before_rss,
                "rss_after_bytes": psutil.Process().memory_info().rss,
            })
    dump(target, {
        "kind": "POST_HOC_COST_SUPPLEMENT_CORRECTING_CANDIDATE_COUNT_ONLY",
        "original_actual_counts_preserved": [46, 296, 1108, 4726],
        "quality_parameters_unchanged": True, "rows": rows,
        "meaning": "implementation cost with generated diverse keys; not storage capacity or TB training evidence",
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args()
    run(options.output, options.report)


if __name__ == "__main__":
    main()
