"""Pending approval state for shell and provider-control pauses."""

from __future__ import annotations

from codey.app.run_registry import RunSnapshot


class ApprovalRegistry:
    def __init__(self) -> None:
        self.pending_shell: dict[str, dict] = {}
        self.pending_teach: dict[str, dict] = {}

    def expire_shell_results(self) -> tuple[dict, ...]:
        stale = list(self.pending_shell.values())
        self.pending_shell.clear()
        return tuple(
            {
                "type": "shell_result",
                "run_id": pending.get("run_id") or "",
                "session_id": pending.get("session_id") or "",
                "id": pending.get("id") or "",
                "approved": False,
                "command": pending.get("command") or "",
                "cwd": pending.get("cwd") or "",
                "output": "任务已停止，该命令的执行批准已过期。",
                "exit_code": None,
            }
            for pending in stale
        )

    def pending_ui_event(self, active: RunSnapshot | None) -> dict | None:
        candidates = [
            pending.get("ui_event")
            for pending in [
                *reversed(tuple(self.pending_teach.values())),
                *reversed(tuple(self.pending_shell.values())),
            ]
            if isinstance(pending.get("ui_event"), dict)
        ]
        if active is not None:
            for event in candidates:
                if event.get("run_id") == active.run_id:
                    return dict(event)
            return None
        return dict(candidates[0]) if candidates else None
