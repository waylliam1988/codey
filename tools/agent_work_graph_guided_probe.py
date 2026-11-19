"""Separate, explicitly guided protocol; never changes a frozen optional run."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.types.json_value import decode_json_object
from tools import agent_work_graph_probe as pilot
from tools import agent_work_graph_query_probe as probe

GUIDANCE = (
    " If work_context is available, query it with view=gaps after reading the required sources and before editing, "
    "then query again after running tests. Compare each returned requirement quote against the implementation and "
    "a concrete counterexample, and add missing regression tests. Static links and a passing suite do not prove "
    "every requirement. If this tool is unavailable, perform the same comparison using the existing tools. "
    "Choose the query terms and all actions yourself."
)
_cases = probe.cases
_hashes = probe.runner_hashes
_prepare = probe.prepare


def cases() -> dict[str, pilot.Case]:
    return {name: replace(case, task=case.task.replace("Modify source only, preserve tests.",
                         "Modify the implementation and add regression tests; preserve existing tests.") + GUIDANCE)
            for name, case in _cases().items()}


def runner_hashes() -> dict[str, str]:
    path = "tools/agent_work_graph_guided_probe.py"
    return {**_hashes(), path: hashlib.sha256((probe.ROOT / path).read_bytes()).hexdigest()}


def prepare(root: Path, repeats: int) -> None:
    _prepare(root, repeats)
    protocol = decode_json_object(json.loads((root / "protocol.json").read_text(encoding="utf-8")))
    protocol["study"] = "explicit-query-guidance"
    protocol["guidance_all_arms"] = GUIDANCE
    protocol["task_amendment"] = "Remove contradictory source-only wording in relay; all arms still preserve existing tests."
    pilot.write(root / "protocol.json", protocol)


def main() -> None:
    with (patch.object(probe, "cases", cases), patch.object(probe, "runner_hashes", runner_hashes),
          patch.object(probe, "prepare", prepare)):
        probe.main()


if __name__ == "__main__":
    main()
