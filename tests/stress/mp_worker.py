"""Child processes for the Level 4 multiprocess test (NOT collected by pytest).

Several real processes share one ``state_home`` at the same time: writers
append while a reader canonicalizes while the parent kills writers. One
honest boundary is baked in here: ``StressWorld`` id counters are process
memory, so only ONE worker performs session writes (production mints
uuids and has no such collision); every worker appends ghost rows under
its own tag, which is unique across processes by construction.

Protocol (stdout, one JSON object per line, always flushed):
  write mode: {"started": tag} ... {"done": tag, "ghost": n, "effects": [...]}
  read mode:  {"started": "reader"} ... {"done": "reader", "reads": m}
Any traceback on stderr + nonzero exit means the child saw corruption or
a deadlock, which is exactly what the parent asserts never happens.
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


def _run_write(state_home: str, tag: str, rounds: int, session_writes: bool, pace: float) -> int:
    from tests.stress.world import StressWorld

    world = StressWorld(Path(state_home), seed=0)
    effects: list[str] = []
    _say({"started": tag})
    for index in range(rounds):
        world.ghost_append(1, tag=tag)
        if session_writes:
            run_id = f"{tag}-{index:04d}"
            world.accept_operation(run_id)
            intent = world.provider_intent(run_id)
            world.provider.mode = "normal"
            world.provider.send({"id": intent.effect_id})
            world.begin_provider(run_id, intent)
            world.settle_provider(run_id, intent.effect_id)
            effects.append(intent.effect_id)
        if pace > 0:
            time.sleep(pace)
    _say({"done": tag, "ghost": rounds, "effects": effects})
    return 0


def _ghost_prefix_closed(rows: list) -> bool:
    """Every tag's numeric suffixes must be 1..k with no gaps."""
    by_tag: dict[str, list[int]] = {}
    for row in rows:
        parts = str(row.get("id", "")).rsplit("-", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            return False
        by_tag.setdefault(parts[0], []).append(int(parts[1]))
    return all(sorted(ids) == list(range(1, len(ids) + 1)) for ids in by_tag.values())


def _run_read(state_home: str, reads: int) -> int:
    from tests.stress.world import StressWorld

    world = StressWorld(Path(state_home), seed=0)
    _say({"started": "reader"})
    max_ghost = 0
    for _ in range(reads):
        facts = world.canonical()
        rows = facts["ghost_rows"]
        assert _ghost_prefix_closed(rows), f"torn ghost view: {[r.get('id') for r in rows][-3:]}"
        max_ghost = max(max_ghost, len(rows))
        time.sleep(0.01)
    _say({"done": "reader", "reads": reads, "max_ghost": max_ghost})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("write", "read"))
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--tag", default="mp")
    parser.add_argument("--rounds", type=int, default=40)
    parser.add_argument("--reads", type=int, default=60)
    parser.add_argument("--session-writes", action="store_true")
    # Deterministic pacing (seconds/round): keeps writers mid-stream while
    # the parent kills them. Asserts nothing about timing.
    parser.add_argument("--pace", type=float, default=0.0)
    args = parser.parse_args(argv)
    if args.mode == "write":
        return _run_write(args.state_home, args.tag, args.rounds, args.session_writes, args.pace)
    return _run_read(args.state_home, args.reads)


if __name__ == "__main__":
    raise SystemExit(main())
