"""Child process for real kill -> restart -> recover tests.

NOT collected by pytest (no test_ prefix): the parent harness spawns it,
waits for a checkpoint file, kills it with ``Popen.kill()``
(TerminateProcess on Windows, SIGKILL on POSIX), then respawns it in
recover mode. Only fsync-backed surfaces are used here, so a kill at an
idle checkpoint loses nothing that ``write()`` already returned for.

Protocol (stdout, one JSON object per line, always flushed):
  write mode:  {"manifest": {...}} then {"checkpoint": path}, then idles.
  recover mode: {"canonical": {...}} then exits 0.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _say(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _run_write(state_home: str, steps: list) -> int:
    from tests.stress.world import StressWorld

    world = StressWorld(Path(state_home), seed=0)
    manifest: dict = {"effects": [], "batches": [], "ghosts": 0}
    checkpoint = ""
    for step in steps:
        action = step.get("do")
        if action == "accept":
            world.accept_operation(step["run"])
        elif action == "begin_provider":
            intent = world.provider_intent(step["run"])
            world.begin_provider(step["run"], intent)
            manifest["effects"].append(intent.effect_id)
        elif action == "settle_provider":
            world.settle_provider(step["run"], step["effect"])
        elif action == "tool_batch":
            batch = world.tool_batch_intent(step["run"], 1, tuple(step["refs"]))
            tool = world.tool_intent(step["run"], step["refs"][0])
            world.begin_batch(step["run"], batch, tool)
            manifest["batches"].append(batch.batch_id)
        elif action == "ghost":
            world.ghost_append(int(step.get("n", 1)), tag=step.get("tag", "kill"))
            manifest["ghosts"] += int(step.get("n", 1))
        elif action == "checkpoint":
            checkpoint = str(step["file"])
            _say({"manifest": manifest})
            Path(checkpoint).write_text("ready\n", encoding="utf-8")
        else:
            raise ValueError(f"unknown step: {action!r}")
    if not checkpoint:
        raise ValueError("write mode needs a checkpoint step")
    while True:
        time.sleep(0.2)


def _run_recover(state_home: str) -> int:
    from tests.stress.world import StressWorld

    world = StressWorld(Path(state_home), seed=0)
    _say({"canonical": world.canonical()})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("write", "recover"))
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--steps", default="[]")
    args = parser.parse_args(argv)
    if args.mode == "write":
        return _run_write(args.state_home, json.loads(args.steps))
    return _run_recover(args.state_home)


if __name__ == "__main__":
    raise SystemExit(main())
