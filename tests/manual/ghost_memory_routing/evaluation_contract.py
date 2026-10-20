"""Frozen, reply-free evaluation rules for the Ghost 0.6.0 pilot.

This module belongs to the manual experiment harness.  It deliberately does
not get imported by production Codey code.  The evaluator fails closed when a
required suite, cell, task family, control, or minimum sample count is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_VERSION = 1
PILOT_SUITE = "modelA"
PILOT_MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"
TASK_PHRASES_PER_FAMILY = 4
REPS_PER_PHRASE = 5
MIN_RUNS_PER_CELL = TASK_PHRASES_PER_FAMILY * REPS_PER_PHRASE


@dataclass(frozen=True)
class FrozenCell:
    family: str
    on_cell: str
    off_cell: str
    task_phrases: int = TASK_PHRASES_PER_FAMILY
    reps_per_phrase: int = REPS_PER_PHRASE

    @property
    def min_runs(self) -> int:
        return self.task_phrases * self.reps_per_phrase


FROZEN_CELLS = (
    FrozenCell("language", "lang_on_d2", "lang_off_d2"),
    FrozenCell("command", "cmd_on_d2", "cmd_off_d2"),
)


def frozen_config() -> dict[str, Any]:
    """Return the committed, reply-free evaluation configuration."""
    return {
        "schema_version": SCHEMA_VERSION,
        "pilot_scope": "single_model_pilot",
        "required_suites": [PILOT_SUITE],
        "model_ids": {PILOT_SUITE: PILOT_MODEL},
        "task_phrases_per_family": TASK_PHRASES_PER_FAMILY,
        "reps_per_phrase": REPS_PER_PHRASE,
        "minimum_runs_per_cell": MIN_RUNS_PER_CELL,
        "cells": [
            {
                "family": cell.family,
                "on": cell.on_cell,
                "off": cell.off_cell,
                "min_runs": cell.min_runs,
            }
            for cell in FROZEN_CELLS
        ],
        "missing_data": "INCOMPLETE; never impute missing controls or samples as zero",
        "default_prompt_changed": False,
        "promotion_scope": "only the required suite; no cross-model claim",
    }


def _entry(cells: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]],
           cell: str, suite: str, family: str) -> Mapping[str, Any] | None:
    entry = cells.get(cell, {}).get(suite, {}).get(family)
    return entry if isinstance(entry, Mapping) else None


def _runs(entry: Mapping[str, Any] | None) -> int:
    try:
        return max(0, int(entry.get("runs", 0))) if entry else 0
    except (TypeError, ValueError):
        return 0


def validate_coverage(
    cells: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]],
) -> dict[str, Any]:
    """Validate the frozen pilot matrix before any family verdict is emitted."""
    missing: list[dict[str, Any]] = []
    for suite in (PILOT_SUITE,):
        for cell in FROZEN_CELLS:
            task_family = "lang" if cell.family == "language" else "cmd"
            for role, name in (("on", cell.on_cell), ("off", cell.off_cell)):
                entry = _entry(cells, name, suite, task_family)
                runs = _runs(entry)
                if runs < cell.min_runs:
                    missing.append({
                        "suite": suite,
                        "family": cell.family,
                        "role": role,
                        "cell": name,
                        "runs": runs,
                        "required_runs": cell.min_runs,
                    })
    return {
        "status": "COMPLETE" if not missing else "INCOMPLETE",
        "required_suites": [PILOT_SUITE],
        "missing": missing,
    }


def _rate(entry: Mapping[str, Any] | None) -> tuple[float, int]:
    runs = _runs(entry)
    if not runs:
        return 0.0, 0
    try:
        passed = max(0, int(entry.get("passed", 0))) if entry else 0
    except (TypeError, ValueError):
        passed = 0
    return min(1.0, passed / runs), runs


def family_verdicts(
    cells: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]],
) -> dict[str, dict[str, Any]]:
    """Return fail-closed, family-specific verdicts for the frozen pilot.

    ``PROMOTE-MATCHED`` is an alias of rank 2 (PROMOTE) for aggregation, while
    the per-suite label is retained in the report for auditability.  An
    incomplete required cell always wins over a weaker behavioural verdict.
    """
    coverage = validate_coverage(cells)
    merged: dict[str, dict[str, Any]] = {"_coverage": coverage}
    rank = {"SHADOW": 0, "REVIEW": 1, "PROMOTE": 2, "PROMOTE-MATCHED": 2}
    for frozen in FROZEN_CELLS:
        task_family = "lang" if frozen.family == "language" else "cmd"
        on, n_on = _rate(_entry(cells, frozen.on_cell, PILOT_SUITE, task_family))
        off, n_off = _rate(_entry(cells, frozen.off_cell, PILOT_SUITE, task_family))
        incomplete = any(
            item["family"] == frozen.family for item in coverage["missing"]
        )
        if incomplete:
            verdict = "INCOMPLETE"
            rank_value = -1
        elif frozen.family == "language":
            verdict = "SHADOW" if on < 0.5 or on <= off else (
                "PROMOTE" if on >= 0.8 else "REVIEW"
            )
            rank_value = rank[verdict]
        else:
            verdict = "PROMOTE-MATCHED" if on >= 0.6 and on > off else (
                "SHADOW" if on <= off else "REVIEW"
            )
            rank_value = rank[verdict]
        merged[frozen.family] = {
            "rule": (
                "promote only when language delivery beats withheld control"
                if frozen.family == "language"
                else "promote only when task-matched command outcome beats control"
            ),
            "suite": PILOT_SUITE,
            "on": f"{on * 100:.0f}% ({n_on}/{frozen.min_runs})",
            "off": f"{off * 100:.0f}% ({n_off}/{frozen.min_runs})",
            "on_rate": on,
            "off_rate": off,
            "on_runs": n_on,
            "off_runs": n_off,
            "per_suite": (
                f"{PILOT_SUITE}: {on * 100:.0f}% vs {off * 100:.0f}% -> {verdict}"
            ),
            "verdict": verdict,
            "rank": rank_value,
        }
    merged["format_length_answer_first"] = {
        "verdict": "WEAK-HINT-ONLY (no compliance evidence)"
    }
    merged["self_description"] = {"verdict": "EXCLUDED by default"}
    merged["hebbian"] = {
        "verdict": "ordering feature + explainability only; no final say"
    }
    return merged


__all__ = [
    "FROZEN_CELLS",
    "MIN_RUNS_PER_CELL",
    "PILOT_MODEL",
    "PILOT_SUITE",
    "family_verdicts",
    "frozen_config",
    "validate_coverage",
]
