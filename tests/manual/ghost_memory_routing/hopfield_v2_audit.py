"""Post-hoc sensitivity checks. Never changes frozen scores, choices or requests.

A bootstrap of all-zero paired stream differences has a degenerate [0, 0]
interval. Report an exact conservative bound on unseen nonzero stream mass
instead of treating that as certainty. Cluster differences are bounded [-1,1].
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import cast

from tests.manual.ghost_memory_routing.hopfield_v2_data import ENCODINGS, MIN_EFFECT, dataset
from tests.manual.ghost_memory_routing.hopfield_v2_study import bootstrap, check, dump, selections


def zero_difference_bound(n: int, alpha: float = 0.0125) -> dict[str, object]:
    """If all n independent cluster differences equal zero, |mean| <= P(X!=0).

    Exact one-sided binomial upper bound after zero nonzero events is
    1-alpha**(1/n). This single event bounds both signs simultaneously.
    Coverage assumes independent draws from the specified history generator.
    """
    if n < 1 or not 0 < alpha < 1:
        raise ValueError("positive cluster count and alpha in (0,1) required")
    radius = 1 - alpha ** (1 / n)
    return {"clusters": n, "interval_level": 1 - alpha, "lower": -radius, "upper": radius,
            "method": "exact conservative unseen-nonzero-cluster-mass bound",
            "excludes_five_point_gain": radius < MIN_EFFECT,
            "assumptions": "iid history streams, paired stream differences in [-1,1]; conditional on fixed generator"}


def positive_mass_upper(n: int, positives: int, alpha: float = 0.0125) -> float:
    """Exact binomial bound: E[paired delta] <= P(delta>0), since delta<=1.

    It intentionally ignores negative outcomes and observed small magnitudes.
    This conservative sensitivity shows what sparse bootstrap support cannot
    rule out, especially with only twelve live history streams.
    """
    if n < 1 or not 0 <= positives <= n or not 0 < alpha < 1:
        raise ValueError("invalid count/alpha")
    if positives == n:
        return 1.0
    if positives == 0:
        return float(1 - alpha ** (1 / n))
    low, high = 0.0, 1.0
    for _ in range(80):
        probability = (low + high) / 2
        cdf = sum(math.comb(n, count) * probability ** count * (1 - probability) ** (n - count)
                  for count in range(positives + 1))
        if cdf > alpha:
            low = probability
        else:
            high = probability
    return (low + high) / 2


def contrasts(rows: list[dict[str, object]], primary: dict[str, str], endpoint: str) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for encoding in ENCODINGS:
        subset = [row for row in rows if row["encoding"] == encoding]
        lookup = {(str(row["query"]), int(cast(int, row["seed"])), str(row["arm"])): row for row in subset}
        for arm in ("hopfield", "anchored"):
            by_stream: dict[str, list[float]] = {}
            for row in subset:
                if row["arm"] != arm:
                    continue
                baseline = lookup[str(row["query"]), int(cast(int, row["seed"])), primary[encoding]]
                by_stream.setdefault(str(row["stream"]), []).append(
                    float(bool(row[endpoint])) - float(bool(baseline[endpoint])))
            deltas = [statistics.mean(values) for _, values in sorted(by_stream.items())]
            exact = zero_difference_bound(len(deltas)) if all(delta == 0 for delta in deltas) else None
            positives = sum(delta > 0 for delta in deltas)
            output.append({"encoding": encoding, "candidate": arm, "baseline": primary[encoding],
                           "endpoint": endpoint, "frozen_bootstrap": bootstrap(deltas),
                           "zero_difference_sensitivity": exact,
                           "conservative_positive_mass_sensitivity": {
                               "positive_clusters": positives,
                               "upper_mean_gain": positive_mass_upper(len(deltas), positives),
                               "confidence": 0.9875,
                               "meaning": "worst case bound, not a replacement primary score or tuned acceptance gate",
                           }})
    return output


def descriptive(rows: list[dict[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for encoding in ENCODINGS:
        encoded: dict[str, object] = {}
        for arm in dict.fromkeys(str(row["arm"]) for row in rows):
            group = [row for row in rows if row["encoding"] == encoding and row["arm"] == arm]
            recall = [float(cast(float, row["recall_at_2"])) for row in group if row["recall_at_2"] is not None]
            encoded[arm] = {
                "n": len(group), "top1_rate": statistics.mean(float(bool(row["top1"])) for row in group),
                "mean_recall_at_2_positive_queries": statistics.mean(recall),
                "per_seed_hit_rate": {
                    str(seed): statistics.mean(float(bool(row["hit"])) for row in group if row["seed"] == seed)
                    for seed in sorted({int(cast(int, row["seed"])) for row in group})
                },
                "action_correct_but_not_grounded": sum(bool(row.get("action_only_correct"))
                                                       and not row.get("grounded", False) for row in group),
            }
        output[encoding] = encoded
    return output


def audit(home: Path, target: Path) -> None:
    check(home)
    primary = {encoding: selection.primary_baseline for encoding, selection in selections(home).items()}
    heldout = [json.loads(line) for line in (home / "heldout-cells.jsonl").read_text(encoding="utf-8").splitlines()]
    lookup = {(row["query"], row["seed"], row["arm"]): row for row in heldout if row["encoding"] == "text"}
    cases = {query.id: (query, gold) for query, gold in dataset("test")}
    failures: list[dict[str, object]] = []
    for row in heldout:
        if row["encoding"] != "text" or row["arm"] != "hopfield" or row["updates"] != 1 or row["hit"]:
            continue
        query, gold = cases[row["query"]]
        selected_ids = set(row["selected"])
        selected = tuple(event for event in query.events if event.id in selected_ids)
        failures.append({
            "query": query.id, "stream": query.stream, "seed": row["seed"],
            "context_explicitly_supplied": bool(query.visible_context),
            "cosine_retrieves_current_correction": lookup[row["query"], row["seed"], "cosine"]["hit"],
            "anchored_recovers": lookup[row["query"], row["seed"], "anchored"]["hit"],
            "selected_only_calibration": bool(selected) and all(event.calibration for event in selected),
            "selected_only_opposite_value": bool(selected) and all(event.action != gold.action for event in selected),
        })
    payload: dict[str, object] = {
        "kind": "POST_HOC_DESCRIPTIVE_AND_CONSERVATIVE_SENSITIVITY_NOT_RETUNING",
        "heldout_descriptive": descriptive(heldout), "heldout_contrasts": contrasts(heldout, primary, "hit"),
        "text_first_correction_hopfield_failures": {
            "stream_seed_pairs": len(failures), "distinct_streams": len({row["stream"] for row in failures}),
            "cosine_has_correction": sum(bool(row["cosine_retrieves_current_correction"]) for row in failures),
            "anchor_recovers": sum(bool(row["anchored_recovers"]) for row in failures),
            "only_old_calibration_selected": sum(bool(row["selected_only_calibration"]) for row in failures),
            "only_old_value_selected": sum(bool(row["selected_only_opposite_value"]) for row in failures),
            "cases": failures,
        },
        "warning": "All-zero bootstrap intervals are not evidence of zero uncertainty or equivalence.",
    }
    live_path = home / "live/result.json"
    if live_path.exists():
        observed = [json.loads(line) for line in (home / "live/cells.jsonl").read_text(encoding="utf-8").splitlines()]
        payload["live_descriptive"] = descriptive(observed)
        payload["live_contrasts"] = contrasts(observed, primary, "grounded")
    dump(target, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args()
    audit(options.output, options.report)


if __name__ == "__main__":
    main()
