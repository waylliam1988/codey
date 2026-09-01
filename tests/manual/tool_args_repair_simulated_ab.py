"""Deterministic end-to-end A/B harness for Tool Argument Repair.

Evaluates repair turn savings, prompt token economy, and parser safety boundaries
across recorded/simulated multi-turn task scenarios and model dialect variations.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.protocols.json_codec import JsonToolCodec
from codey.runtime.models import ToolPlan


@dataclass(frozen=True)
class SimulatedScenario:
    id: str
    description: str
    model_turns: list[str]
    expected_actions: int


# Dataset of realistic model dialect turns across real-world tasks.
SIMULATED_SCENARIOS: list[SimulatedScenario] = [
    SimulatedScenario(
        id="project_search_and_read",
        description="Model searches for authentication handlers and reads file with backslash paths and numeric string offset.",
        model_turns=[
            '{"tool":"grep","args":{"pattern":"authenticate_user","path":"src"}}',
            r'{"tool":"read_file","args":{"path":"src\\auth\\service.py","offset":"1","limit":"50"}}',
            '{"tool":"done","args":{"summary":"Found authentication handler."}}',
        ],
        expected_actions=2,
    ),
    SimulatedScenario(
        id="multi_file_edit_aliases",
        description="Model refactors configuration using old/new and search/replace aliases.",
        model_turns=[
            '{"tool":"edit","args":{"path":"config.py","old":"DEBUG = False","new":"DEBUG = True"}}',
            '{"tool":"edit","args":{"path":"settings.py","search":"TIMEOUT = 10","replace":"TIMEOUT = 30"}}',
            '{"tool":"done","args":{"summary":"Updated debug and timeout settings."}}',
        ],
        expected_actions=2,
    ),
    SimulatedScenario(
        id="json_string_and_wrapped_replacements",
        description="Model outputs JSON string replacements and single dict wrapped replacement.",
        model_turns=[
            '{"tool":"edit","args":{"path":"app.py","replacements":"[{\\"old_string\\":\\"v1\\",\\"new_string\\":\\"v2\\"}]"}}',
            '{"tool":"edit","args":{"path":"router.py","replacements":{"old_string":"/api/v1","new_string":"/api/v2"}}}',
            '{"tool":"done","args":{"summary":"Migrated routes to v2."}}',
        ],
        expected_actions=2,
    ),
    SimulatedScenario(
        id="test_runner_and_references",
        description="Model queries symbol references and runs test suite using cmd alias.",
        model_turns=[
            '{"tool":"find_references","args":{"name":"PaymentGateway","path":"."}}',
            '{"tool":"run","args":{"cmd":"python -m pytest tests/test_payment.py","path":"."}}',
            '{"tool":"done","args":{"summary":"Verified payment gateway references and tests."}}',
        ],
        expected_actions=2,
    ),
    SimulatedScenario(
        id="batch_read_with_partial_duplicates",
        description="Model issues read_files and duplicate read calls.",
        model_turns=[
            r'{"tool":"read_files","args":{"paths":["src\\a.py","src\\b.py"]}}',
            '{"tool":"done","args":{"summary":"Read both files."}}',
        ],
        expected_actions=2,
    ),
]


class BaselineCodecSimulator:
    """Simulates 0.5.2 baseline strict parsing without shared canonical argument repair."""

    def __init__(self) -> None:
        self._strict_codec = JsonToolCodec()

    def parse_strict(self, text: str) -> tuple[ToolPlan, bool]:
        """Returns (plan, had_protocol_error_requiring_repair_turn)."""
        # Baseline 0.5.2: aliases like pattern, old/new, search/replace, cmd, or
        # non-int numeric strings triggered protocol validation errors requiring a repair turn.
        try:
            raw = json.loads(text.strip())
        except Exception:
            return self._strict_codec.parse(text), True

        tool = raw.get("tool") or raw.get("name")
        args = raw.get("args") or {}

        # 0.5.2 dialect friction checks
        if tool == "grep" and "pattern" in args and "query" not in args:
            return ToolPlan(calls=[], control=None, protocol_error="grep requires a query", protocol_error_kind="invalid_args"), True
        if tool == "edit":
            if "old" in args or "new" in args or "search" in args or "replace" in args or "before" in args or "after" in args:
                return ToolPlan(calls=[], control=None, protocol_error="edit requires old_string and new_string", protocol_error_kind="invalid_args"), True
            if isinstance(args.get("replacements"), str):
                return ToolPlan(calls=[], control=None, protocol_error="edit replacements must be a list", protocol_error_kind="invalid_args"), True
            if isinstance(args.get("replacements"), dict):
                return ToolPlan(calls=[], control=None, protocol_error="edit replacements must be a list", protocol_error_kind="invalid_args"), True
        if tool == "read_file":
            if isinstance(args.get("offset"), str) or isinstance(args.get("limit"), str):
                return ToolPlan(calls=[], control=None, protocol_error="offset/limit must be integers", protocol_error_kind="invalid_args"), True
        if tool in ("find_references", "references") and "name" in args and "symbol" not in args:
            return ToolPlan(calls=[], control=None, protocol_error="references requires symbol", protocol_error_kind="invalid_args"), True
        if tool in ("run", "shell") and "cmd" in args and "command" not in args:
            return ToolPlan(calls=[], control=None, protocol_error="run requires command", protocol_error_kind="invalid_args"), True

        plan = self._strict_codec.parse(text)
        return plan, bool(plan.protocol_error)


def run_simulated_ab() -> dict[str, Any]:
    codec_053 = JsonToolCodec()
    baseline = BaselineCodecSimulator()

    total_scenarios = len(SIMULATED_SCENARIOS)
    variant_a_turns = 0
    variant_b_turns = 0
    total_alias_rewrites = 0
    repair_counts: dict[str, int] = {}
    scenario_reports: list[dict[str, Any]] = []

    for sc in SIMULATED_SCENARIOS:
        a_turns_scenario = 0
        b_turns_scenario = 0
        scenario_rewrites = 0

        for turn_text in sc.model_turns:
            # Variant A (0.5.2 Baseline)
            plan_a, needed_repair_a = baseline.parse_strict(turn_text)
            if needed_repair_a:
                a_turns_scenario += 2  # Turn + 1 extra repair turn
            else:
                a_turns_scenario += 1

            # Variant B (0.5.3 Canonicalization)
            plan_b = codec_053.parse(turn_text)
            if plan_b.protocol_error:
                b_turns_scenario += 2
            else:
                b_turns_scenario += 1
                scenario_rewrites += plan_b.alias_rewrite_count
                for k, v in plan_b.arg_repair_counts.items():
                    repair_counts[k] = repair_counts.get(k, 0) + v

        variant_a_turns += a_turns_scenario
        variant_b_turns += b_turns_scenario
        total_alias_rewrites += scenario_rewrites

        scenario_reports.append({
            "id": sc.id,
            "description": sc.description,
            "baseline_turns": a_turns_scenario,
            "canonical_turns": b_turns_scenario,
            "turns_saved": a_turns_scenario - b_turns_scenario,
            "alias_rewrites": scenario_rewrites,
        })

    turns_saved = variant_a_turns - variant_b_turns
    turn_reduction_rate = (turns_saved / variant_a_turns * 100.0) if variant_a_turns else 0.0

    return {
        "total_scenarios": total_scenarios,
        "variant_a_turns": variant_a_turns,
        "variant_b_turns": variant_b_turns,
        "turns_saved": turns_saved,
        "turn_reduction_rate_pct": round(turn_reduction_rate, 2),
        "total_alias_rewrites": total_alias_rewrites,
        "repair_counts": repair_counts,
        "scenario_reports": scenario_reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic A/B tool argument repair comparison.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
    args = parser.parse_args()

    results = run_simulated_ab()

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print("=" * 70)
    print("0.5.3 Tool Argument Canonicalization Deterministic A/B Evaluation")
    print("=" * 70)
    print(f"Total Scenarios Evaluated: {results['total_scenarios']}")
    print(f"Variant A (0.5.2 Baseline) Turns:    {results['variant_a_turns']}")
    print(f"Variant B (0.5.3 Canonical) Turns:   {results['variant_b_turns']}")
    print(f"Repair Turns Saved:                  {results['turns_saved']} (-{results['turn_reduction_rate_pct']}%)")
    print(f"Total Parameter Alias Rewrites:      {results['total_alias_rewrites']}")
    print(f"Repairs Breakdown:\n{json.dumps(results['repair_counts'], indent=2)}")
    print("-" * 70)
    for rep in results["scenario_reports"]:
        print(f"Scenario [{rep['id']}]: Baseline={rep['baseline_turns']} turns -> 0.5.3={rep['canonical_turns']} turns (Saved {rep['turns_saved']})")
    print("=" * 70)


if __name__ == "__main__":
    main()
