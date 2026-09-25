"""Soak scheduler: a deterministic random state machine (Durability Lab Level 2).

P4 drives one fixed lifecycle per run (accept, then exactly one phase-legal
step). The soak scheduler instead keeps cross-step lifecycle state and
picks the next operation from what is actually legal *right now*::

    if provider_pending:
        candidates += [PROVIDER_SETTLE]
    if tool_pending:
        candidates += [TOOL_EXECUTE...]
    if shell_approval_pending:
        candidates += [SHELL_ALLOW, SHELL_STOP]

Every decision flows through a single seeded ``random.Random`` owned by
``world.faults`` (one new ``weighted`` method was added to
``FaultController`` for this; it still never touches business state). The
same seed replays the same failure, step for step.

Three rules keep generation honest:

1. The generator reads scheduler-side lifecycle state, never wall-clock
   time and never OS threads. Timing-sensitive paths (real browser
   timeouts, real thread races) stay in the P2 tests; here they would be
   flaky by construction.
2. The executor (``execute_step``) never consults randomness: every choice
   is already frozen in the recorded step dict, so a recorded script
   replays byte-identically (see ``replay_script`` and ``shrink.py``).
3. Faults are per-area weighted, and clusters (timeout -> restart ->
   retry -> duplicate -> restart -> settle) are first-class: with a small
   probability a step schedules a scripted cluster instead of one op.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from typing import Any
from unittest import mock

from codey.app import shell_service
from codey.app.approval_registry import ApprovalRegistry
from codey.automation.browser_worker import BrowserWorker
from codey.providers.diagnostics import ProviderFailure
from codey.providers.supervisor import STATE_OPEN, ProviderHealth
from codey.repairs.self_repair import SelfRepairSupervisor
from codey.runtime.core import cancellation
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectError,
    RuntimeEffectIntent,
)
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    compute_batch_digest,
)
from tests.stress.model import fold_event_rows
from tests.stress.oracle import InvariantChecker
from tests.stress.world import FakeProviderTimeout

# -- step kinds ----------------------------------------------------------

ACCEPT = "accept"
PROVIDER_SEND = "provider_send"
PROVIDER_SETTLE = "provider_settle"
TOOL_BEGIN = "tool_begin"
TOOL_DUP = "tool_dup"
GHOST_APPEND = "ghost_append"
GHOST_REPLAY = "ghost_replay"
SSE_EMIT = "sse_emit"
SSE_RECONNECT = "sse_reconnect"
SSE_OVERFLOW = "sse_overflow"
SHELL_ALLOW = "shell_allow"
SHELL_STOP = "shell_stop"
BROWSER_CALL = "browser_call"
BROWSER_SUBMIT = "browser_submit"
REPAIR_ENQUEUE = "repair_enqueue"
REPAIR_RUN = "repair_run"
EXPIRE_APPROVALS = "expire_approvals"
TICK = "tick"
RESTART = "restart"

# Area selection weights: provider-heavy, restart-rare but constant.
AREA_WEIGHTS = (
    ("provider", 30),
    ("tool", 20),
    ("ghost", 15),
    ("sse", 10),
    ("shell", 10),
    ("repair", 5),
    ("browser", 5),
    ("clock", 3),
    ("restart", 2),
)

# Per-area fault weights. Normal dominates; the danger zones get their own
# slice instead of sharing one global probability.
PROVIDER_MODE_WEIGHTS = (
    ("normal", 78),
    ("timeout_before_execute", 10),
    ("execute_then_timeout", 12),
)
SHELL_VERDICT_WEIGHTS = (("allow", 70), ("stop", 20), ("stop_before_claim", 10))
SSE_FAULT_WEIGHTS = (("emit", 80), ("disconnect", 10), ("overflow", 5), ("dup_emit", 5))

CLUSTER_PROBABILITY = 0.05


# -- shell harness (also used by the P2 race test) ------------------------

def shell_ctx(approvals: ApprovalRegistry) -> SimpleNamespace:
    """Minimal context the shell service needs for claim/execute."""
    return SimpleNamespace(
        _shell_spawn_gate=threading.Lock(),
        lock=threading.Lock(),
        approvals=approvals,
        run_registry=SimpleNamespace(stop_flag=threading.Event()),
        approval_generation=approvals.current_generation,
    )


def shell_pending_request(command: str, run_id: str, project: str = ".") -> dict:
    return {
        "id": "shell-soak",
        "run_id": run_id,
        "session_id": "stress-session",
        "command": command,
        "cwd": ".",
        "project": project,
    }


def counting_spawn(spawns: list, order: list, *, stamp: Any = None):
    """Fake Popen: records the spawn, returns an already-done process.

    ``stamp`` optionally replaces the ``"spawn"`` marker (the P2 race test
    records ``perf_counter_ns`` so the oracle can order spawn vs stop).
    """
    proc = mock.Mock()
    proc.stdout = "ok"
    proc.stderr = ""
    proc.returncode = 0
    proc.stdout_truncated = False
    proc.stderr_truncated = False
    proc.stdout_bytes = 2
    proc.stderr_bytes = 0

    def _start(*args: object, **kwargs: object):
        # Runs inside the spawn gate: gate-serialized with Stop's section,
        # so the "spawn"/"stop" order is exact (no clock needed).
        spawns.append(stamp() if stamp is not None else "spawn")
        order.append("spawn")
        return proc, mock.Mock()

    def _wait(
        proc_arg: object,
        _job: object,
        _command: str,
        _timeout: float,
        *args: object,
        **kwargs: object,
    ):
        return proc

    return _start, _wait


def _drain(sub: Any) -> list:
    events = []
    while True:
        try:
            events.append(sub.get_nowait())
        except queue.Empty:
            return events


def _repair_failure(provider: str) -> ProviderFailure:
    return ProviderFailure(
        provider, "send", "", "", "response_missing", "now", "response_missing"
    )


def _ok_repair(job: Any) -> Any:
    """Deterministic repair runner: plain values only, never Mock.

    The supervisor journals ``ok/generation/error`` off the result object;
    Mock auto-attributes carry memory addresses and would poison canonical
    determinism.
    """
    from codey.repairs.adapter_repair import AdapterRepairResult

    return AdapterRepairResult(ok=True, provider_id=job.provider_id, generation=0, error="")


def _repair_health() -> ProviderHealth:
    return ProviderHealth(state=STATE_OPEN, last_failure_kind="response_missing")


# -- scheduler ------------------------------------------------------------

class SoakScheduler:
    """Generates legal next steps from lifecycle state, seeded and replayable."""

    def __init__(self, seed: int) -> None:
        from tests.stress.faults import FaultController

        self.seed = int(seed)
        self.faults = FaultController(int(seed))
        self._run_seq = 0
        self._ghost_tag_seq = 0
        self._sse_seq = 0
        self._approval_seq = 0
        self.provider_pending: dict[str, tuple[str, str]] = {}  # effect_id -> (run_id, mode)
        # batch_id -> (run_id, ref, tool_effect): dup retries must resend the
        # IDENTICAL batch (same digest); same id with different content is a
        # conflict, a different question production answers with TransitionError.
        self.tool_pending: dict[str, tuple[str, str, str]] = {}
        # One run holds one operation leaf: provider/tool sends are legal
        # only from "running"; a pending provider must settle first.
        self.run_phase: dict[str, str] = {}
        self.shell_pending: dict[str, str] = {}  # approval_id -> run_id
        self.open_runs: list[str] = []
        self.sse_emitted: list[str] = []
        self.sse_cursor = 0
        self.repair_open = False
        self._queued: list[dict] = []
        self._cluster: list[dict] = []

    # -- ids ---------------------------------------------------------------

    def _next_run(self) -> str:
        self._run_seq += 1
        return f"soak-{self._run_seq:06d}"

    # -- generation ----------------------------------------------------------

    def next_step(self, world: Any) -> dict:
        """Return the next recorded step; every choice is rng-driven.

        Generation is pure except world id counters (effect/batch ids are
        minted here so steps can name them; replay re-mints them in the
        same order). Run acceptance is an explicit queued ACCEPT step, so
        the recorded script contains every durable write.
        """
        while not self._queued and not self._cluster:
            self._generate(world)
        if self._queued:
            return self._queued.pop(0)
        return self._cluster.pop(0)

    def _generate(self, world: Any) -> None:
        if self.faults.rng.random() < CLUSTER_PROBABILITY:
            cluster = self._maybe_cluster(world)
            if cluster:
                self._cluster = cluster
                return
        area = self.faults.weighted("area", AREA_WEIGHTS)
        step = self._step_for_area(world, str(area))
        if step is not None:
            self._queued.append(step)

    def _use_run(self, world: Any) -> str:
        if self.open_runs and self.faults.rng.random() < 0.7:
            return self.faults.rng.choice(self.open_runs)
        return self._accept_run(world)

    def _accept_run(self, world: Any) -> str:
        run_id = self._next_run()
        self.open_runs.append(run_id)
        if len(self.open_runs) > 64:
            self.open_runs = self.open_runs[-64:]
        self.run_phase[run_id] = "running"
        # The durable accept is an explicit step, never a generation side effect.
        self._queued.append({"op": ACCEPT, "run": run_id, "faults": []})
        return run_id

    def _legal_run(self, world: Any, *phases: str) -> str:
        """Draw an open run in one of ``phases``; accept a fresh run if none."""
        legal = [run for run in self.open_runs if self.run_phase.get(run) in phases]
        if legal and self.faults.rng.random() < 0.8:
            return self.faults.rng.choice(legal)
        return self._accept_run(world)

    def _step_for_area(self, world: Any, area: str) -> dict | None:
        if area == "provider":
            return self._provider_step(world)
        if area == "tool":
            return self._tool_step(world)
        if area == "ghost":
            return self._ghost_step(world)
        if area == "sse":
            return self._sse_step(world)
        if area == "shell":
            return self._shell_step(world)
        if area == "repair":
            return self._repair_step(world)
        if area == "browser":
            return {"op": self.faults.weighted("browser", ((BROWSER_CALL, 90), (BROWSER_SUBMIT, 10))), "faults": []}
        if area == "clock":
            return self._clock_step(world)
        if area == "restart":
            return {"op": RESTART, "faults": ["restart"]}
        return None

    def _provider_step(self, world: Any) -> dict:
        # Only clean sends may settle as ok: timed-out intents stay unknown,
        # exactly like P4 (settling them ok would be fake success).
        clean = sorted(
            effect for effect, (_, mode) in self.provider_pending.items() if mode == "normal"
        )
        if clean and self.faults.rng.random() < 0.45:
            effect_id = self.faults.rng.choice(clean)
            run_id = self.provider_pending[effect_id][0]
            dup = self.faults.rng.random() < 0.05
            return {
                "op": PROVIDER_SETTLE, "run": run_id, "effect": effect_id,
                "status": "ok", "error_code": "", "dup": dup,
                "faults": ["dup_settle"] if dup else [],
            }
        run_id = self._legal_run(world, "running")
        intent = world.provider_intent(run_id)
        mode = self.faults.weighted("provider-mode", PROVIDER_MODE_WEIGHTS)
        skip_settle = mode == "normal" and self.faults.rng.random() < 0.05
        return {
            "op": PROVIDER_SEND, "run": run_id, "effect": intent.effect_id,
            "mode": mode, "skip_settle": skip_settle,
            "faults": [f"provider:{mode}"] if mode != "normal" else (["no_settle"] if skip_settle else []),
        }

    def _tool_step(self, world: Any) -> dict:
        if self.tool_pending and self.faults.rng.random() < 0.12:
            batch_id = self.faults.rng.choice(sorted(self.tool_pending))
            run_id, ref, tool_effect = self.tool_pending[batch_id]
            return {
                "op": TOOL_DUP, "run": run_id, "batch": batch_id,
                "ref": ref, "tool_effect": tool_effect, "faults": ["dup_begin"],
            }
        run_id = self._legal_run(world, "running", "tool_pending")
        ref = f"ref-{self._run_seq}-{len(self.tool_pending)}"
        batch = world.tool_batch_intent(run_id, 1, (ref,))
        tool = world.tool_intent(run_id, ref)
        return {
            "op": TOOL_BEGIN, "run": run_id, "batch": batch.batch_id,
            "tool_effect": tool.effect_id, "ref": ref, "faults": [],
        }

    def _ghost_step(self, world: Any) -> dict:
        if self.faults.rng.random() < 0.15:
            return {"op": GHOST_REPLAY, "faults": []}
        self._ghost_tag_seq += 1
        return {
            "op": GHOST_APPEND, "n": self.faults.rng.randint(1, 4),
            "tag": f"soak{self._ghost_tag_seq}", "faults": [],
        }

    def _sse_step(self, world: Any) -> dict:
        fault = self.faults.weighted("sse", SSE_FAULT_WEIGHTS)
        if fault == "disconnect":
            return {"op": SSE_RECONNECT, "faults": ["disconnect"]}
        if fault == "overflow":
            return {"op": SSE_OVERFLOW, "faults": ["overflow"]}
        self._sse_seq += 1
        key = f"sse-{self._sse_seq:06d}"
        dup = 1 if fault == "dup_emit" else 0
        return {"op": SSE_EMIT, "key": key, "dup": dup, "faults": ["dup_emit"] if dup else []}

    def _shell_step(self, world: Any) -> dict:
        verdict = self.faults.weighted("shell", SHELL_VERDICT_WEIGHTS)
        if verdict == "allow" and self.shell_pending:
            approval_id = self.faults.rng.choice(sorted(self.shell_pending))
            return {
                "op": SHELL_ALLOW, "approval": approval_id,
                "run": self.shell_pending[approval_id], "faults": [],
            }
        self._approval_seq += 1
        approval_id = f"shell-soak-{self._approval_seq:06d}"
        run_id = self._use_run(world)
        self.shell_pending[approval_id] = run_id
        if verdict == "stop_before_claim":
            return {
                "op": SHELL_STOP, "approval": approval_id, "run": run_id,
                "before_claim": True, "faults": ["stop_before_claim"],
            }
        if verdict == "stop":
            return {
                "op": SHELL_STOP, "approval": approval_id, "run": run_id,
                "before_claim": False, "faults": ["stop_race"],
            }
        return {"op": SHELL_ALLOW, "approval": approval_id, "run": run_id, "faults": []}

    def _repair_step(self, world: Any) -> dict | None:
        if self.repair_open:
            ok = self.faults.rng.random() < 0.8
            return {"op": REPAIR_RUN, "ok": ok, "faults": [] if ok else ["runner_crash"]}
        if self.faults.rng.random() < 0.5:
            return None
        provider = self.faults.rng.choice(["qwen", "deepseek", "mock"])
        self.repair_open = True
        return {"op": REPAIR_ENQUEUE, "provider": provider, "faults": []}

    def _clock_step(self, world: Any) -> dict:
        delta = self.faults.rng.randint(1, 10_000)
        if self.shell_pending and self.faults.rng.random() < 0.2:
            run_id = self.faults.rng.choice(sorted(set(self.shell_pending.values())))
            return {"op": EXPIRE_APPROVALS, "run": run_id, "delta": delta, "faults": ["time_passes"]}
        return {"op": TICK, "delta": delta, "faults": []}

    def self_check(self, world: Any) -> None:
        """Cross-check harness memory against durable reads.

        Called at every periodic check and at the end of a run/replay. The
        scheduler must never become a second Runtime: what it believes is
        pending must equal what the bytes on disk say is pending. (Ghost
        ids are execution-ordered and intentionally not tracked here;
        ghost stability is covered by restarts + canonical.)
        """
        oracle = InvariantChecker()
        durable_provider: list[str] = []
        for run_id in self.open_runs:
            durable_provider.extend(world.pending_provider_ids(run_id))
        oracle.check_model_matches_durable(
            "provider_pending", sorted(self.provider_pending), durable_provider
        )
        durable_batches: list[str] = []
        for run_id in self.open_runs:
            durable_batches.extend(
                batch.intent.batch_id for batch in world.undelivered_batches(run_id)
            )
        oracle.check_model_matches_durable(
            "tool_batches",
            sorted(batch for batch, _ in self._tool_runs()),
            durable_batches,
        )

    def _tool_runs(self) -> list[tuple[str, str]]:
        return [(batch_id, run_id) for batch_id, (run_id, _, _) in self.tool_pending.items()]

    # -- clusters --------------------------------------------------------------

    def _maybe_cluster(self, world: Any) -> list[dict]:
        kind = self.faults.weighted(
            "cluster",
            (("provider_crash_retry", 40), ("ghost_kill_rebuild", 30), ("duplicate_restart", 30)),
        )
        if kind == "provider_crash_retry":
            return self._cluster_provider_crash_retry(world)
        if kind == "ghost_kill_rebuild":
            return self._cluster_ghost_kill_rebuild(world)
        return self._cluster_duplicate_restart(world)

    def _cluster_provider_crash_retry(self, world: Any) -> list[dict]:
        # The state machine holds one leaf per run: the timed-out intent must
        # settle (as error, never ok) before the same run may send again.
        run_id = self._accept_run(world)
        intent = world.provider_intent(run_id)
        retry = world.provider_intent(run_id)
        return [
            {"op": PROVIDER_SEND, "run": run_id, "effect": intent.effect_id,
             "mode": "execute_then_timeout", "skip_settle": False,
             "faults": ["cluster:provider:after_send_timeout"]},
            {"op": RESTART, "faults": ["cluster:restart"]},
            {"op": PROVIDER_SETTLE, "run": run_id, "effect": intent.effect_id,
             "status": "error", "error_code": "timeout", "dup": False,
             "faults": ["cluster:settle_original_error"]},
            {"op": PROVIDER_SEND, "run": run_id, "effect": retry.effect_id,
             "mode": "normal", "skip_settle": False, "faults": ["cluster:retry"]},
            {"op": PROVIDER_SETTLE, "run": run_id, "effect": retry.effect_id,
             "status": "ok", "error_code": "", "dup": False, "faults": ["cluster:settle_retry"]},
        ]

    def _cluster_ghost_kill_rebuild(self, world: Any) -> list[dict]:
        self._ghost_tag_seq += 1
        tag = f"c{self._ghost_tag_seq}"
        return [
            {"op": GHOST_APPEND, "n": 3, "tag": f"{tag}-a", "faults": ["cluster:ghost_append"]},
            {"op": RESTART, "faults": ["cluster:restart"]},
            {"op": GHOST_APPEND, "n": 2, "tag": f"{tag}-b", "faults": ["cluster:ghost_append"]},
            {"op": RESTART, "faults": ["cluster:restart"]},
            {"op": GHOST_REPLAY, "faults": ["cluster:ghost_rebuild"]},
        ]

    def _cluster_duplicate_restart(self, world: Any) -> list[dict]:
        run_id = self._accept_run(world)
        ref = f"cref-{self._run_seq}"
        batch = world.tool_batch_intent(run_id, 1, (ref,))
        tool = world.tool_intent(run_id, ref)
        return [
            {"op": TOOL_BEGIN, "run": run_id, "batch": batch.batch_id,
             "tool_effect": tool.effect_id, "ref": ref, "faults": ["cluster:tool_begin"]},
            {"op": TOOL_DUP, "run": run_id, "batch": batch.batch_id,
             "tool_effect": tool.effect_id, "ref": ref, "faults": ["cluster:dup_begin"]},
            {"op": RESTART, "faults": ["cluster:restart"]},
            {"op": GHOST_APPEND, "n": 1, "tag": f"cdup{self._run_seq}", "faults": ["cluster:post_restart"]},
        ]


# -- execution context -----------------------------------------------------

class SoakContext:
    """Harness-owned handles the world does not keep: browser, repair, books."""

    def __init__(self, state_home: object) -> None:
        from pathlib import Path

        self.state_home = Path(state_home)
        self.oracle = InvariantChecker()
        self.browser = BrowserWorker(name="soak-browser", max_queue_size=8)
        self.supervisor: SelfRepairSupervisor | None = None
        self.committed: list[str] = []
        self.rejected: list[str] = []
        self.unknowns: list[tuple[str, str]] = []
        self.counts: dict[str, int] = {}
        self.fault_counts: dict[str, int] = {}
        self.restarts = 0
        self.spawns = 0

    def note(self, step: dict) -> None:
        self.counts[step["op"]] = self.counts.get(step["op"], 0) + 1
        for fault in step.get("faults", ()):
            self.fault_counts[fault] = self.fault_counts.get(fault, 0) + 1

    def repair_supervisor(self) -> SelfRepairSupervisor:
        if self.supervisor is None:
            self.supervisor = SelfRepairSupervisor(self.state_home, runner=_ok_repair)
        return self.supervisor

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self.browser.close()


# -- executor: randomness-free, script-replayable ---------------------------

def execute_step(scheduler: SoakScheduler, world: Any, ctx: SoakContext, step: dict) -> None:
    """Run one recorded step. Uses only ``step`` data: replay-safe."""
    op = step["op"]
    ctx.note(step)
    if op == ACCEPT:
        world.accept_operation(step["run"])
        # Lifecycle bookkeeping, mirrored from generation: the original run
        # already appended during next_step (skip), replay rebuilds it here.
        if step["run"] not in scheduler.open_runs:
            scheduler.open_runs.append(step["run"])
            if len(scheduler.open_runs) > 64:
                scheduler.open_runs = scheduler.open_runs[-64:]
        scheduler.run_phase[step["run"]] = "running"
    elif op == PROVIDER_SEND:
        _exec_provider_send(scheduler, world, ctx, step)
    elif op == PROVIDER_SETTLE:
        _exec_provider_settle(scheduler, world, ctx, step)
    elif op == TOOL_BEGIN:
        _exec_tool_begin(scheduler, world, ctx, step)
    elif op == TOOL_DUP:
        _exec_tool_dup(world, ctx, step)
    elif op == GHOST_APPEND:
        world.ghost_append(step["n"], tag=step["tag"])
    elif op == GHOST_REPLAY:
        rows = tuple(world.ghost_rows())
        ctx.oracle.check_replay_idempotent(fold_event_rows, rows, rows)
    elif op == SSE_EMIT:
        _exec_sse_emit(scheduler, world, step)
    elif op == SSE_RECONNECT:
        _exec_sse_reconnect(scheduler, world)
    elif op == SSE_OVERFLOW:
        _exec_sse_overflow(world)
    elif op == SHELL_ALLOW:
        _exec_shell_allow(scheduler, world, ctx, step)
    elif op == SHELL_STOP:
        _exec_shell_stop(scheduler, world, ctx, step)
    elif op == BROWSER_CALL:
        assert ctx.browser.call(lambda: "soak-ok", timeout=10) == "soak-ok"
    elif op == BROWSER_SUBMIT:
        accepted = sum(1 for _ in range(3) if ctx.browser.submit(lambda: None))
        assert accepted >= 1
        assert ctx.browser.health_snapshot().queue_size <= 8
    elif op == REPAIR_ENQUEUE:
        ctx.repair_supervisor().maybe_enqueue(step["provider"], _repair_failure(step["provider"]), _repair_health())
    elif op == REPAIR_RUN:
        _exec_repair_run(scheduler, ctx, step)
    elif op == EXPIRE_APPROVALS:
        world.clock.now += step["delta"]
        world.approvals.expire_shell_results(run_id=step["run"])
    elif op == TICK:
        world.clock.now += step["delta"]
    elif op == RESTART:
        _exec_restart(scheduler, world, ctx)
    else:  # pragma: no cover - generator only emits known ops
        raise ValueError(f"unknown soak op: {op!r}")


def _provider_intent_for(world: Any, step: dict) -> RuntimeEffectIntent:
    """Rebuild the generation-minted intent without touching world counters."""
    return RuntimeEffectIntent(
        effect_id=step["effect"],
        effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
        session_id=world.session_id,
        run_id=step["run"],
        turn=step.get("turn", 1),
        provider_id=step.get("provider_id", "mock"),
    )


def _tool_pair_for(world: Any, step: dict) -> tuple:
    """Rebuild the generation-minted batch + tool intents, counter-free."""
    items = (
        DeliveryBatchItem(tool_index=0, tool_name="read", ref=step["ref"], replay_class="safe"),
    )
    batch = DeliveryBatchIntent(
        batch_id=step["batch"], session_id=world.session_id, run_id=step["run"],
        turn=step.get("turn", 1), items=items, batch_digest=compute_batch_digest(items),
    )
    tool = RuntimeEffectIntent(
        effect_id=step["tool_effect"],
        effect_category=EFFECT_CATEGORY_TOOL_CALL,
        session_id=world.session_id,
        run_id=step["run"],
        phase="writer",
        turn=step.get("turn", 1),
        tool_index=0,
        tool_name="read",
        replay_class="safe",
    )
    return batch, tool


def _exec_provider_send(scheduler: SoakScheduler, world: Any, ctx: SoakContext, step: dict) -> None:
    intent = _provider_intent_for(world, step)
    world.provider.mode = step["mode"]
    try:
        world.provider.send({"id": intent.effect_id})
    except FakeProviderTimeout:
        if step["mode"] == "timeout_before_execute":
            ctx.rejected.append(intent.effect_id)
            return
        world.begin_provider(step["run"], intent)
        ctx.committed.append(intent.effect_id)
        scheduler.provider_pending[intent.effect_id] = (step["run"], step["mode"])
        scheduler.run_phase[step["run"]] = "provider_pending"
        ctx.unknowns.append((intent.effect_id, "pending"))
        return
    world.begin_provider(step["run"], intent)
    ctx.committed.append(intent.effect_id)
    if step.get("skip_settle"):
        scheduler.provider_pending[intent.effect_id] = (step["run"], step["mode"])
        scheduler.run_phase[step["run"]] = "provider_pending"
        ctx.unknowns.append((intent.effect_id, "pending"))
    else:
        world.settle_provider(step["run"], intent.effect_id)


def _exec_provider_settle(scheduler: SoakScheduler, world: Any, ctx: SoakContext, step: dict) -> None:
    world.settle_provider(
        step["run"], step["effect"],
        status=step.get("status", "ok"), error_code=step.get("error_code", ""),
    )
    scheduler.provider_pending.pop(step["effect"], None)
    scheduler.run_phase[step["run"]] = "running"
    if step.get("dup"):
        # Settling twice with the same status is an idempotent no-op.
        world.settle_provider(
            step["run"], step["effect"],
            status=step.get("status", "ok"), error_code=step.get("error_code", ""),
        )


def _exec_tool_begin(scheduler: SoakScheduler, world: Any, ctx: SoakContext, step: dict) -> None:
    batch, tool = _tool_pair_for(world, step)
    world.begin_batch(step["run"], batch, tool)
    ctx.committed.append(batch.batch_id)
    scheduler.tool_pending[batch.batch_id] = (step["run"], step["ref"], step["tool_effect"])
    scheduler.run_phase[step["run"]] = "tool_pending"


def _exec_tool_dup(world: Any, ctx: SoakContext, step: dict) -> None:
    # Resend the byte-identical batch + tool: duplicate begin must raise
    # RuntimeEffectError (same path as P4), never commit a second fact.
    batch, tool = _tool_pair_for(world, step)
    try:
        world.begin_batch(step["run"], batch, tool)
    except RuntimeEffectError:
        ctx.rejected.append(step["batch"] + ":dup")
    else:  # pragma: no cover - the invariant under test
        raise AssertionError(f"duplicate tool begin committed twice: {step['batch']}")


def _exec_sse_emit(scheduler: SoakScheduler, world: Any, step: dict) -> None:
    # The bus cursor counts every bus event, duplicates included.
    for _ in range(1 + step.get("dup", 0)):
        world.sse_emit(step["key"], type="tool")
        scheduler.sse_emitted.append(step["key"])
        scheduler.sse_cursor += 1


def _exec_sse_reconnect(scheduler: SoakScheduler, world: Any) -> None:
    """Disconnect at cursor, replay the suffix: must equal what was emitted."""
    replayed = world.bus.replay_events_after(scheduler.sse_cursor)
    if replayed and replayed[0][1].get("type") == "resync_required":
        scheduler.sse_cursor = len(scheduler.sse_emitted)
        return  # Window expired: resync marker, never silent loss.
    keys = [payload.get("event_key", "") for _, payload in replayed]
    assert keys == scheduler.sse_emitted[scheduler.sse_cursor:], (
        f"sse suffix mismatch: {keys} != {scheduler.sse_emitted[scheduler.sse_cursor:]}"
    )
    scheduler.sse_cursor = len(scheduler.sse_emitted)


def _exec_sse_overflow(world: Any) -> None:
    # An isolated bus: overflow probes the resync marker without shifting
    # the world bus sequence the reconnect cursor tracks.
    from codey.app.event_bus import EventBus

    bus = EventBus(replay_limit=512)
    sub = bus.subscribe(maxsize=2)
    try:
        for index in range(10):
            bus.emit({"event_key": f"overflow-{index}", "type": "tool"})
        kinds = [event.get("type") for event in _drain(sub)]
        assert "resync_required" in kinds, f"overflow without resync marker: {kinds}"
    finally:
        bus.unsubscribe(sub)


def _exec_shell_allow(scheduler: SoakScheduler, world: Any, ctx: SoakContext, step: dict) -> None:
    world.approvals.add_shell(
        step["approval"], shell_pending_request(f"cmd-{step['approval']}", step["run"])
    )
    ctx_obj = shell_ctx(world.approvals)
    spawns: list = []
    order: list = []
    start, wait = counting_spawn(spawns, order)
    pending, ticket = shell_service.claim_shell_ticket(
        ctx_obj, step["approval"], timeout=30, output_limit=1000
    )
    assert ticket is not None, f"allow claimed nothing: {step['approval']}"
    with (
        mock.patch.object(cancellation, "start_process", side_effect=start),
        mock.patch.object(cancellation, "wait_process", side_effect=wait),
    ):
        result = shell_service.execute_shell_ticket(ctx_obj, ticket)
    assert result["ok"]
    assert spawns == ["spawn"], f"allow must spawn exactly once: {spawns}"
    ctx.spawns += 1
    scheduler.shell_pending.pop(step["approval"], None)
    ctx.oracle.check_stop_allow_linearized([0.0], None)


def _exec_shell_stop(scheduler: SoakScheduler, world: Any, ctx: SoakContext, step: dict) -> None:
    world.approvals.add_shell(
        step["approval"], shell_pending_request(f"cmd-{step['approval']}", step["run"])
    )
    ctx_obj = shell_ctx(world.approvals)
    if step.get("before_claim"):
        world.approvals.expire_shell_results()
        pending, ticket = shell_service.claim_shell_ticket(
            ctx_obj, step["approval"], timeout=30, output_limit=1000
        )
        assert ticket is None, "stop-before-claim minted a ticket"
        ctx.oracle.check_stop_allow_linearized([], 0.0)
    else:
        pending, ticket = shell_service.claim_shell_ticket(
            ctx_obj, step["approval"], timeout=30, output_limit=1000
        )
        with ctx_obj._shell_spawn_gate:
            ctx_obj.run_registry.stop_flag.set()
            world.approvals.expire_shell_results()
        # Stop won after claim: executing must not spawn.
        spawns: list = []
        order: list = []
        start, wait = counting_spawn(spawns, order)
        if ticket is not None:
            with (
                mock.patch.object(cancellation, "start_process", side_effect=start),
                mock.patch.object(cancellation, "wait_process", side_effect=wait),
            ):
                result = shell_service.execute_shell_ticket(ctx_obj, ticket)
            assert result["status"] == "stopped", f"stop-then-execute was not stopped: {result}"
        assert spawns == [], f"stop-then-execute spawned: {spawns}"
        ctx.oracle.check_stop_allow_linearized([], 0.0)
    scheduler.shell_pending.pop(step["approval"], None)


def _exec_repair_run(scheduler: SoakScheduler, ctx: SoakContext, step: dict) -> None:
    supervisor = ctx.repair_supervisor()
    if step.get("ok"):
        supervisor.runner = _ok_repair
    else:
        def _crash(job: object) -> object:
            raise RuntimeError("soak runner crash")

        supervisor.runner = _crash
    supervisor.run_pending_once()
    if step.get("ok"):
        scheduler.repair_open = False


def _exec_restart(scheduler: SoakScheduler, world: Any, ctx: SoakContext) -> None:
    ghost_before = [row["id"] for row in world.ghost_rows()]
    world.restart()
    ctx.supervisor = None
    ctx.restarts += 1
    ghost_after = [row["id"] for row in world.ghost_rows()]
    ctx.oracle.check_ghost_stable_across_restart(ghost_before, ghost_after)
    # Memory-only surfaces rebuild empty by design: cursors reset, never resumed.
    scheduler.sse_emitted = []
    scheduler.sse_cursor = 0


# -- script replay ----------------------------------------------------------

def replay_script(script: list[dict], state_home: object, *, check_every: int = 0) -> dict:
    """Execute a recorded script on a fresh world. No randomness consulted."""
    from pathlib import Path

    from tests.stress.world import StressWorld

    world = StressWorld(Path(state_home), seed=0)
    scheduler = SoakScheduler(seed=0)
    ctx = SoakContext(world.state_home)
    oracle = InvariantChecker()
    for index, step in enumerate(script):
        try:
            execute_step(scheduler, world, ctx, step)
        except Exception as exc:
            raise SoakFailure(index, step, exc) from exc
        if check_every and (index + 1) % check_every == 0:
            scheduler.self_check(world)
            facts = oracle.check_recovery_idempotent(world.canonical)
            oracle.assert_valid(facts, unknowns=ctx.unknowns)
    scheduler.self_check(world)
    facts = oracle.check_recovery_idempotent(world.canonical)
    oracle.assert_valid(facts, unknowns=ctx.unknowns)
    oracle.check_no_duplicate_facts([c for c in ctx.committed if not c.endswith(":dup")])
    return {
        "facts": facts,
        "committed": ctx.committed,
        "rejected": ctx.rejected,
        "unknowns": ctx.unknowns,
        "counts": ctx.counts,
        "fault_counts": ctx.fault_counts,
        "restarts": ctx.restarts,
    }


class SoakFailure(Exception):
    """A soak step failed; carries the step index for seeds and shrink."""

    def __init__(self, index: int, step: dict, cause: BaseException) -> None:
        super().__init__(f"soak step {index} {step!r} failed: {cause!r}")
        self.index = index
        self.step = step
        self.cause = cause
        self.script: list[dict] = []


__all__ = [
    "ACCEPT",
    "BROWSER_CALL",
    "BROWSER_SUBMIT",
    "CLUSTER_PROBABILITY",
    "EXPIRE_APPROVALS",
    "GHOST_APPEND",
    "GHOST_REPLAY",
    "PROVIDER_SEND",
    "PROVIDER_SETTLE",
    "REPAIR_ENQUEUE",
    "REPAIR_RUN",
    "RESTART",
    "SHELL_ALLOW",
    "SHELL_STOP",
    "SSE_EMIT",
    "SSE_OVERFLOW",
    "SSE_RECONNECT",
    "TICK",
    "TOOL_BEGIN",
    "TOOL_DUP",
    "SoakContext",
    "SoakFailure",
    "SoakScheduler",
    "counting_spawn",
    "execute_step",
    "replay_script",
    "shell_ctx",
    "shell_pending_request",
]
