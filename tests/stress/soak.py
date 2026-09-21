"""Durability Lab Level 2 runner: long random fault soak with deterministic replay.

Usage::

    python -m tests.stress.soak --seed 827361 --operations 100000
    python -m tests.stress.soak --tier nightly
    python -m tests.stress.soak --seed 827361 --operations 100000 --no-shrink

Tiers are calibrated to ~80 durable ops/s on this machine (every op
fsyncs; that is the point). Override with ``--operations``::

    pr:      1,000 ops  (~1 min,  gate-safe)
    nightly: 10,000 ops (~6 min)
    weekly:  100,000 ops (~1 h)

One session log caps at 4 MB of compacted spine (production backpressure:
loud ``RuntimeLogWriteError``, never silent loss), which bounds one epoch
to a few thousand mixed ops. Larger runs split into epochs
(``--epoch-size``, default 4000): each epoch is a fresh session under a
derived seed, deterministically replayable on its own.

A failure prints the seed, the step index and the fault, saves the full
recorded script as an artifact, and (unless ``--no-shrink``) reduces it
with ``shrink.py`` before exiting nonzero. Rerunning the same seed and
operation count replays the identical failure::

    python -m tests.stress.soak --seed <seed> --operations <n>
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import traceback
from pathlib import Path

from tests.stress.model import canonical_json
from tests.stress.oracle import InvariantChecker
from tests.stress.scheduler import SoakContext, SoakFailure, SoakScheduler, execute_step
from tests.stress.world import StressWorld

TIERS = {"pr": 1_000, "nightly": 10_000, "weekly": 100_000}


def run_soak(
    seed: int,
    operations: int,
    *,
    state_home: Path | None = None,
    check_every: int = 500,
    collect_script: bool = True,
) -> dict:
    """Drive the scheduler; return the debrief (raises ``SoakFailure``)."""
    tmp = None
    if state_home is None:
        tmp = tempfile.TemporaryDirectory()
        state_home = Path(tmp.name) / "state"
    try:
        world = StressWorld(state_home, seed=seed)
        scheduler = SoakScheduler(seed)
        scheduler.faults = world.faults
        ctx = SoakContext(world.state_home)
        oracle = InvariantChecker(seed=seed)
        script: list[dict] = []
        started = time.perf_counter()
        try:
            for index in range(operations):
                step = scheduler.next_step(world)
                if collect_script:
                    script.append(step)
                try:
                    execute_step(scheduler, world, ctx, step)
                except Exception as exc:
                    failure = SoakFailure(index, step, exc)
                    failure.script = script
                    raise failure from exc
                if check_every and (index + 1) % check_every == 0:
                    scheduler.self_check(world)
                    facts = oracle.check_recovery_idempotent(world.canonical)
                    oracle.assert_valid(facts, unknowns=ctx.unknowns)
            scheduler.self_check(world)
            facts = oracle.check_recovery_idempotent(world.canonical)
            oracle.assert_valid(facts, unknowns=ctx.unknowns)
            oracle.check_no_duplicate_facts(
                [c for c in ctx.committed if not c.endswith(":dup")]
            )
        finally:
            elapsed = time.perf_counter() - started
        return {
            "seed": seed,
            "operations": operations,
            "elapsed_s": round(elapsed, 1),
            "facts": facts,
            "canonical": canonical_json(facts),
            "committed": ctx.committed,
            "rejected": ctx.rejected,
            "unknowns": ctx.unknowns,
            "counts": ctx.counts,
            "fault_counts": ctx.fault_counts,
            "restarts": ctx.restarts,
            "spawns": ctx.spawns,
            "script": script,
        }
    finally:
        if tmp is not None:
            tmp.cleanup()


def merge_debriefs(debriefs: list[dict]) -> dict:
    """Aggregate per-epoch debriefs into one report (epochs stay replayable)."""
    merged: dict = {
        "seed": debriefs[0]["seed"],
        "operations": sum(d["operations"] for d in debriefs),
        "elapsed_s": round(sum(d["elapsed_s"] for d in debriefs), 1),
        "epochs": len(debriefs),
        "committed": [c for d in debriefs for c in d["committed"]],
        "rejected": [c for d in debriefs for c in d["rejected"]],
        "unknowns": [c for d in debriefs for c in d["unknowns"]],
        "counts": {},
        "fault_counts": {},
        "restarts": sum(d["restarts"] for d in debriefs),
        "spawns": sum(d.get("spawns", 0) for d in debriefs),
        "canonicals": [d["canonical"] for d in debriefs],
    }
    for key in ("counts", "fault_counts"):
        for debrief in debriefs:
            for name, count in debrief[key].items():
                merged[key][name] = merged[key].get(name, 0) + count
    return merged


def format_report(debrief: dict, *, shrunk: int | None = None, replay_verified: bool = False) -> str:
    counts = debrief["counts"]
    faults = debrief["fault_counts"]
    lines = [
        "",
        f"Seed: {debrief['seed']}",
        "",
        f"Operations:        {debrief['operations']:>10,}",
        f"Epochs:            {debrief.get('epochs', 1):>10,}",
        f"Restarts:          {debrief['restarts']:>10,}",
        f"Faults:            {sum(faults.values()):>10,}",
        f"Committed facts:   {len(debrief['committed']):>10,}",
        f"Rejected dupes:    {len(debrief['rejected']):>10,}",
        f"Unknown outcomes:  {len(debrief.get('unknowns', [])):>10,}",
        f"Shell spawns:      {debrief.get('spawns', 0):>10,}",
        "",
        "Top operations:",
    ]
    for name, count in sorted(counts.items(), key=lambda item: -item[1])[:8]:
        lines.append(f"  {name:<18} {count:>10,}")
    lines.append("Top faults:")
    for name, count in sorted(faults.items(), key=lambda item: -item[1])[:8]:
        lines.append(f"  {name:<18} {count:>10,}")
    lines.extend([
        "",
        "Canonical replay:        PASS",
        "Recovery idempotence:    PASS",
        f"Deterministic replay:    {'PASS' if replay_verified else 'NOT CHECKED'}",
    ])
    if shrunk is not None:
        lines.append(f"Shrunk repro:            {shrunk} steps (see artifact)")
    lines.append(f"Elapsed:               {debrief['elapsed_s']}s")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Durability Lab Level 2 soak.")
    parser.add_argument("--seed", type=int, default=827361)
    parser.add_argument("--operations", type=int, default=0)
    parser.add_argument("--tier", choices=sorted(TIERS), default="")
    parser.add_argument("--check-every", type=int, default=500)
    # Ignored by .gitignore: failure scripts can be tens of thousands of
    # lines and must never be tracked. Resolved against the package dir so
    # the CLI behaves the same from any cwd.
    parser.add_argument(
        "--artifacts",
        default=str(Path(__file__).resolve().parent / "artifacts"),
    )
    parser.add_argument("--shrink", dest="shrink", action="store_true", default=True)
    parser.add_argument("--no-shrink", dest="shrink", action="store_false")
    parser.add_argument("--shrink-budget", type=int, default=120)
    parser.add_argument("--epoch-size", type=int, default=4000)
    parser.add_argument("--verify-replay", dest="verify_replay", action="store_true", default=None)
    parser.add_argument("--no-verify-replay", dest="verify_replay", action="store_false")
    args = parser.parse_args(argv)
    operations = args.operations or TIERS.get(args.tier, TIERS["pr"])
    epoch_size = max(1, args.epoch_size)
    verify = args.verify_replay if args.verify_replay is not None else operations <= 2000
    debriefs: list[dict] = []
    remaining = operations
    epoch = 0
    while remaining > 0:
        epoch_ops = min(epoch_size, remaining)
        epoch_seed = args.seed + epoch
        try:
            debrief = run_soak(epoch_seed, epoch_ops, check_every=args.check_every)
        except SoakFailure as failure:
            return _handle_failure(args, failure, epoch_seed, epoch_ops, epoch)
        if verify:
            rerun = run_soak(epoch_seed, epoch_ops, check_every=0, collect_script=False)
            if rerun["canonical"] != debrief["canonical"]:
                print(f"\nDETERMINISTIC REPLAY MISMATCH in epoch {epoch}: same seed, different facts")
                return 1
        debriefs.append(debrief)
        remaining -= epoch_ops
        epoch += 1
    print(format_report(merge_debriefs(debriefs), replay_verified=verify))
    return 0


def _handle_failure(
    args: argparse.Namespace, failure: SoakFailure, seed: int, operations: int, epoch: int,
) -> int:
    from tests.stress.shrink import shrink_script

    print(f"\nSOAK FAILURE at step {failure.index}: {failure.step!r}")
    print("Cause:")
    traceback.print_exception(failure.cause)
    # The recorded prefix rides on the failure: no re-run needed, and the
    # artifact never depends on the crashed run's memory beyond the script.
    script: list[dict] = list(getattr(failure, "script", []) or [failure.step])
    shrunk: list[dict] | None = None
    if args.shrink:
        started = time.perf_counter()
        try:
            shrunk = shrink_script(
                script, failure, budget_s=args.shrink_budget, check_every=0,
            )
            print(f"\nShrunk {len(script)} steps -> {len(shrunk)} steps "
                  f"in {time.perf_counter() - started:.1f}s")
        except Exception as exc:  # Shrink is best-effort; the artifact is the guarantee.
            print(f"\nShrink failed (best-effort only): {exc!r}")
    artifact = {
        "seed": seed,
        "epoch": epoch,
        "failed_at": failure.index,
        "step": failure.step,
        "cause": repr(failure.cause),
        "script": script,
        "shrunk": shrunk,
    }
    path = Path(args.artifacts) / f"soak-failure-seed{seed}-step{failure.index}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"Artifact: {path}")
    print(f"Replay: python -m tests.stress.soak --seed {seed} "
          f"--operations {failure.index + 1} --no-shrink")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
