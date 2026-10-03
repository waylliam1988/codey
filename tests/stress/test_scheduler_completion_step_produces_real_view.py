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

from codey.operations.task_loop import KernelExecutionDeps, KernelObservationDeps, KernelRunRequest, KernelTransportDeps

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
    # Keep decisive completion facts including content identity; only
    # relativize temp roots (environment differences), never drop fingerprints.
    str_roots: list[str] = []
    for root in roots or ():
        try:
            text = str(root or "").strip()
        except Exception:
            continue
        if text:
            str_roots.append(text)
    # Longest prefix first so nested tmp paths relativize correctly.
    str_roots.sort(key=len, reverse=True)

    def _relativize(text: object) -> object:
        if not isinstance(text, str) or not text:
            return text
        out = text
        for root in str_roots:
            if root and root in out:
                out = out.replace(root, "<root>")
        # Also relativize bare tmp base names that survive drive differences.
        return out

    obs = []
    for row in normalized.get("verification_observations", []) or []:
        if not isinstance(row, dict):
            continue
        obs.append({
            "command": _relativize(row.get("command")),
            "cwd": _relativize(row.get("cwd")),
            "passed": row.get("passed"),
            "exit_code": row.get("exit_code"),
            "revision": row.get("revision"),
            "workspace_revision": row.get("workspace_revision"),
            "workspace_fingerprint": row.get("workspace_fingerprint"),
        })
    obs.sort(key=lambda r: (str(r["command"]), str(r["cwd"])))
    checks = []
    for c in normalized.get("proof_checks", []) or []:
        if isinstance(c, dict):
            checks.append((c.get("check_id"), c.get("status")))
        elif isinstance(c, (list, tuple)) and len(c) == 2:
            checks.append((c[0], c[1]))
        else:
            checks.append((str(c), ""))
    checks.sort(key=lambda pair: (str(pair[0]), str(pair[1])))
    return {
        "completed": normalized.get("completed"),
        "proof_checks": checks,
        "verification_observations": obs,
        "verification_identity_valid": normalized.get("verification_identity_valid"),
        "verification_revision": normalized.get("verification_revision"),
        "workspace_revision": normalized.get("workspace_revision"),
        "verification_fingerprint": normalized.get("verification_fingerprint"),
        "workspace_fingerprint": normalized.get("workspace_fingerprint"),
        "verification_required": normalized.get("verification_required"),
        "required_checks_passed": normalized.get("required_checks_passed"),
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


def test_real_pytest_assertion_failure_blocks_with_failed_observation(tmp_path, monkeypatch):
    """Real failing test file (assert 1 == 2) must block as failed, not unknown."""
    import json

    monkeypatch.setenv("NATIVE_TOOLS", "0")

    from codey.agents.tools import DEFAULT_TOOL_FNS
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolResult
    from codey.workspace.revision import WorkspaceRevisionStore

    project = tmp_path / "assert-fail"
    project.mkdir(parents=True, exist_ok=True)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    (project / "test_fail.py").write_text("def test_fail():\n    assert 1 == 2\n", encoding="utf-8")
    store = WorkspaceRevisionStore(tmp_path / "state")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(project),
        max_turns=6,
    )
    base = store.current_state(str(project), ignored_paths=())
    session.set_workspace_state(int(base.revision or 0), base.fingerprint)

    replies = [
        json.dumps({"tool": "edit", "args": {"path": "a.py", "content": "x = 1\n"}}),
        json.dumps({"tool": "run", "args": {"command": "python -m pytest test_fail.py -q", "path": "."}}),
        json.dumps({"tool": "done", "args": {"summary": "done"}}),
    ]
    it = iter(replies)

    class _Web:
        def new_chat(self):
            pass

        def send(self, _prompt):
            return next(it)

        def close(self):
            pass

    def _edit(call):
        (project / str(call.args.get("path") or "a.py")).write_text(
            str(call.args.get("content") or ""), encoding="utf-8"
        )
        return ToolResult(ok=True, call=call, model_text="edited", audit={"changed": True})

    run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=_Web(),
                provider_id="web",
                run_id="r-assert-fail",
                user_task="fix",
            ),
            execution=KernelExecutionDeps(
                executors={"edit": _edit},
                project_path=project,
                tool_fns=DEFAULT_TOOL_FNS,
                workspace_revision_store=store,
            ),
            observation=KernelObservationDeps(
                completion_context=None,
            ),
        ),
    )
    assert session.verifications, "failing pytest must record an observation"
    last = session.verifications[-1]
    assert last.get("passed") is False
    assert type(last.get("exit_code")) is int and last.get("exit_code") != 0
    assert evaluate(session, "done", context=None).complete is False
