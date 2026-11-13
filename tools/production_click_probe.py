"""Live production clicks after fixed, real source acquisition; no click A/B."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.types.json_value import JsonObject, JsonValue
from tools import production_navigation_ab as navigation
from tools.browser_production_live_gate import select_target
from tools.completion_explanation_ab import ExperimentProvider, Target, production_source_digest, write_json


def run_probe(directory: Path, scenario: navigation.Scenario, expected: navigation.ExpectedFlow,
              target: Target, *, max_turns: int = 10) -> JsonObject:
    prefix: JsonObject = {"tool": "codemode", "args": {"code":
        "unwrap(await tools.web_search({query:inputs.query}));return unwrap(await tools.open_url({url:inputs.url}));",
        "inputs": {"query": scenario.source.question, "url": expected.entry}}}

    def provider(model: Target, output: Path, *, fixed_acquisition: bool) -> ExperimentProvider:
        return ExperimentProvider(model, output, fixed_acquisition=True, acquisition_action=prefix)

    with patch.object(navigation, "ExperimentProvider", provider):
        result = navigation.run_arm(directory, scenario, expected, "A", target, max_turns=max_turns)
    result.update(scope="fixed_source_acquisition_live_production_click_regression", source_selection_live=False,
                  scripted_prefix=prefix, source_interface_arm="A", arm="production_click_regression")
    write_json(directory / "summary.json", result)
    return result


class Options(argparse.Namespace):
    run_dir: Path
    pairs: int
    max_turns: int
    base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Options())
    if args.pairs < 1 or args.max_turns < 1:
        parser.error("pairs and max-turns must be positive")
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = select_target(120, args.base_url)
    before = production_source_digest()
    rows: list[JsonValue] = []
    for pair in range(args.pairs):
        for scenario, expected in navigation.production_cases():
            if scenario.kind == "search":
                continue
            print(f"Starting click regression {pair + 1}/{scenario.source.name}", flush=True)
            row = run_probe(root / f"pair-{pair + 1}" / scenario.source.name, scenario, expected,
                replace(target, seed=target.seed + pair), max_turns=args.max_turns)
            rows.append(row)
            progress: JsonObject = {key: row[key] for key in ("case", "passed", "stop_reason", "model_calls")}
            print(json.dumps(progress), flush=True)
            write_json(root / "progress.json", rows)
    after = production_source_digest()
    write_json(root / "summary.json", {"model": target.model, "production_before": before,
        "production_after": after, "production_unchanged": before == after, "results": rows})
    return 0 if before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
