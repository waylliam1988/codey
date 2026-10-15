"""Durable approval fixture using the same artifact contract as the hook."""

from codey.runtime.effects.shell_approval_records import ShellApprovalFact


def seed_requested_approval(ctx, approval_id="approval", *, continue_after=True, command="echo approval"):
    artifact = ctx.managed_outputs.write_json_artifact(
        session_id="session",
        run_id="source",
        payload={
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
        approval_id, "session", "source", "requested", command_ref=artifact.handle, command_digest=artifact.sha256
    )
    ctx.record_shell_approval_fact(fact)
    return fact
