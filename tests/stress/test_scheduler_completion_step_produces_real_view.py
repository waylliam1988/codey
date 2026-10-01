"""Scheduler completion steps produce real gate views through the kernel chain.

Locks P2: a deterministic ``completion_produce`` scheduler step must run a
real read/edit/verify/done chain (actual file write, actual workspace
fingerprint, actual gate evaluation), append exactly one real view via
``SoakContext.record_real_completion_view``, and pass the truthfulness
oracle. Failure/unknown variants must never produce a passing view. The
generator must be able to emit the step, and ``replay_script`` must replay
it byte-identically.
"""
from __future__ import annotations

COMPLETION_OP = "completion_produce"


def _world_scheduler_ctx(tmp_path, seed=7):
    from tests.stress.scheduler import SoakContext, SoakScheduler
    from tests.stress.world import StressWorld

    state_home = tmp_path / "state"
    state_home.mkdir(parents=True, exist_ok=True)
    world = StressWorld(state_home, seed=seed)
    scheduler = SoakScheduler(seed=seed)
    ctx = SoakContext(world.state_home)
    return world, scheduler, ctx


def test_execute_completion_step_produces_real_view(tmp_path):
    from tests.stress.oracle import InvariantChecker
    from tests.stress.scheduler import execute_step

    world, scheduler, ctx = _world_scheduler_ctx(tmp_path)
    try:
        step = {
            "op": COMPLETION_OP, "seq": 1,
            "relpath": "completion_000001.py",
            "content": "x_1 = 1\n",
            "outcome": "pass",
            "faults": [],
        }
        execute_step(scheduler, world, ctx, step)
        assert len(ctx.completion_views) == 1
        view = ctx.completion_views[0]
        assert view["completed"] is True
        assert view["verification_observations"], "real view must carry observations"
        for obs in view["verification_observations"]:
            assert obs["exit_code"] == 0
            assert obs["passed"] is True
        # The file chain really ran: project file exists with exact content.
        project_dir = tmp_path / "state" / "completion" / "run-000001"
        written = project_dir / "completion_000001.py"
        assert written.is_file()
        assert written.read_text(encoding="utf-8") == "x_1 = 1\n"
        # The oracle really checks this view (not zero-iteration wiring).
        calls: list[dict] = []
        checker = InvariantChecker()
        original = checker.check_completion_truthful

        def _track(v: dict) -> None:
            calls.append(v)
            return original(v)

        checker.check_completion_truthful = _track  # type: ignore[method-assign]
        checker.assert_valid(
            {"log_rows": [], "ghost_rows": []},
            unknowns=[],
            completion_views=ctx.completion_views,
        )
        assert len(calls) == 1
    finally:
        ctx.close()


def test_completion_failure_and_unknown_never_pass(tmp_path):
    from tests.stress.scheduler import execute_step

    for seq, outcome in ((2, "fail"), (3, "unknown")):
        world, scheduler, ctx = _world_scheduler_ctx(tmp_path / f"case-{seq}", seed=seq)
        try:
            step = {
                "op": COMPLETION_OP, "seq": seq,
                "relpath": f"completion_{seq:06d}.py",
                "content": f"x_{seq} = 1\n",
                "outcome": outcome,
                "faults": [],
            }
            execute_step(scheduler, world, ctx, step)
            assert len(ctx.completion_views) == 1
            view = ctx.completion_views[0]
            assert view["completed"] is False, f"outcome={outcome} must not complete"
        finally:
            ctx.close()


def test_scheduler_generation_can_emit_completion(tmp_path):
    world, scheduler, ctx = _world_scheduler_ctx(tmp_path, seed=12345)
    try:
        seen = False
        for _ in range(2000):
            step = scheduler.next_step(world)
            if step.get("op") == COMPLETION_OP:
                seen = True
                break
        assert seen, "soak generation never emitted a completion step in 2000 draws"
    finally:
        ctx.close()


def _normalize_completion_view(view: dict, *roots: object) -> dict:
    import copy

    normalized = copy.deepcopy(view)
    # Compare the decisive completion facts only; temp paths never decide.
    obs = []
    for row in normalized.get("verification_observations", []) or []:
        obs.append({
            "command": row.get("command"),
            "cwd": row.get("cwd"),
            "passed": row.get("passed"),
            "exit_code": row.get("exit_code"),
            "revision": row.get("revision"),
            "workspace_revision": row.get("workspace_revision"),
        })
    obs.sort(key=lambda r: (str(r["command"]), str(r["cwd"])))
    return {
        "completed": normalized.get("completed"),
        "proof_checks": sorted(
            (c.get("check_id"), c.get("status"))
            for c in normalized.get("proof_checks", []) or []
        ),
        "verification_observations": obs,
        "verification_identity_valid": normalized.get("verification_identity_valid"),
    }


def test_completion_script_replays_identically(tmp_path):
    from tests.stress.scheduler import execute_step, replay_script

    world, scheduler, ctx = _world_scheduler_ctx(tmp_path / "orig", seed=9)
    try:
        script = [
            {"op": COMPLETION_OP, "seq": 11,
             "relpath": "completion_000011.py",
             "content": "x_11 = 1\n",
             "outcome": "pass", "faults": []},
        ]
        for step in script:
            execute_step(scheduler, world, ctx, step)
        assert len(ctx.completion_views) == 1
        orig_view = _normalize_completion_view(ctx.completion_views[0], tmp_path / "orig")
    finally:
        ctx.close()
    # Replay on a fresh home must rebuild the same completion facts
    # (command, cwd, result, identity, checks, completed), not just counts.
    replay_home = tmp_path / "replay-state"
    replay_home.mkdir(parents=True, exist_ok=True)
    result = replay_script(script, replay_home)
    assert result["counts"].get(COMPLETION_OP) == 1
    assert len(result.get("completion_views", [])) == 1
    replay_view = _normalize_completion_view(result["completion_views"][0], replay_home)
    assert replay_view == orig_view
