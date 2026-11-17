"""Frozen wire-schema contrast; reuse original tasks, review and strict scorer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.manual.ghost_memory_routing.world_model_verification_review import ACTIONS, Options, run_live


def response_format() -> dict[str, object]:
    properties: dict[str, object] = {"action": {"type": "string", "enum": list(ACTIONS)}}
    schema: dict[str, object] = {
        "type": "object", "properties": properties, "required": ["action"], "additionalProperties": False,
    }
    definition: dict[str, object] = {"name": "world_model_review_action", "strict": True, "schema": schema}
    return {"type": "json_schema", "json_schema": definition}


def run_structured(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "request-config.json"
    if config_path.exists() or (home / "metrics.json").exists() or (home / "failure.json").exists():
        raise ValueError("refusing to overwrite earlier structured evidence")
    config = response_format()
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    run_live(home, response_format=config, require_first_valid=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    options = Options()
    parser.parse_args(namespace=options)
    if not options.live:
        raise ValueError("explicit --live is required for this model experiment")
    run_structured(options.output)


if __name__ == "__main__":
    main()
