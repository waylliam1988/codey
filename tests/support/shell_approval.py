"""Durable approval fixture using the same artifact contract as the hook."""

import hashlib

from codey.agents.shell_approval import shell_command_payload
from codey.policies.command_line import resolve_shell
from codey.policies.shell_analysis import analyze_shell
from codey.runtime.effects.shell_approval_records import ShellApprovalFact


def admit_shell_task(ctx, *, session_id: str, run_id: str, task: str = "shell approval",
                     provider_id: str = "local", project: str | None = None) -> None:
    """Create the canonical durable task required by an approval fixture."""
    try:
        ctx.durable.task(run_id)
        return
    except KeyError:
        pass
    selection = None
    registry = getattr(ctx, "run_registry", None)
    lookup_selection = getattr(registry, "api_selection_for", None)
    if callable(lookup_selection):
        selection = lookup_selection(run_id)
    from codey.providers.snapshot import provider_snapshot
    ctx.durable.admit_input(
        conversation_id=session_id,
        request_id=run_id,
        task_id=run_id,
        text=task,
        provider_snapshot=provider_snapshot(provider_id, selection),
        task_state={"task": task, "provider_id": provider_id, "project": project},
    )
    # Shell approval is emitted while the assistant turn is waiting for an
    # external tool outcome.  Keep the fixture on that canonical checkpoint
    # so tests exercise the same effect admission boundary as production.
    ctx.durable.start_task(run_id, state={})
    ctx.durable.advance_task(task_id=run_id, phase="assistant_response_pending", state={})


def add_pending_approval(ctx, approval_id: str, pending: dict[str, object]) -> None:
    """Seed the UI projection from a fully identified durable approval."""
    row = dict(pending)
    shell = resolve_shell()
    row["shell"] = shell.to_payload()
    event = row.get("ui_event")
    if isinstance(event, dict):
        row.setdefault("session_id", event.get("session_id"))
        row.setdefault("run_id", event.get("run_id"))
        row.setdefault("command", event.get("command", ""))
        row.setdefault("cwd", event.get("cwd", "."))
    row["analysis"] = analyze_shell(str(row.get("command") or ""), shell).to_payload()
    session_id = str(row.get("session_id") or "")
    run_id = str(row.get("run_id") or "")
    if session_id and run_id:
        admit_shell_task(ctx, session_id=session_id, run_id=run_id,
                         task=str(row.get("task") or "shell approval"),
                         provider_id=str(row.get("provider") or "local"),
                         project=str(row.get("project")) if row.get("project") else None)
    fields = shell_command_payload(str(row.get("command") or ""))
    row.update({key: value for key, value in fields.items() if key != "command"})
    row["command_preview"] = fields["command"]
    if getattr(ctx, "managed_outputs", None) is not None:
        artifact = ctx.managed_outputs.write_json_artifact(
        session_id=session_id, run_id=run_id, payload=row,
        )
        row.update(command_ref=artifact.handle, command_digest=artifact.sha256)
    else:
        row.update(command_ref=f"fixture:{approval_id}", command_digest=hashlib.sha256(
            str(row.get("command") or "").encode("utf-8")).hexdigest())
    ctx.add_pending_shell_approval(approval_id, row)


def seed_requested_approval(ctx, approval_id="approval", *, continue_after=True, command="echo approval", source_run_id="source"):
    # Approval lifecycle facts belong to the admitted durable task.  The
    # JSONL store is only an audit projection and must not be enough to make a
    # shell request executable in tests or production.
    admit_shell_task(ctx, session_id="session", run_id=source_run_id, task="shell approval source")
    shell = resolve_shell()
    artifact = ctx.managed_outputs.write_json_artifact(
        session_id="session",
        run_id=source_run_id,
        payload={
            "shell": shell.to_payload(),
            "analysis": analyze_shell(command, shell).to_payload(),
            "command": command,
            "cwd": ".",
            "project": str(ctx.state_home),
            "attached_project": None,
            "intent": "chat",
            "provider": "local",
            "continue_after": continue_after,
            "max_turns": 3,
        },
    )
    fact = ShellApprovalFact(
        approval_id, "session", source_run_id, "requested", command_ref=artifact.handle, command_digest=artifact.sha256
    )
    ctx.record_shell_approval_fact(fact)
    return fact


__all__ = ["add_pending_approval", "admit_shell_task", "seed_requested_approval"]
