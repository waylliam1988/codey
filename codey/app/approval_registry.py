"""Pending approval state for shell and provider-control pauses."""

from __future__ import annotations

from codey.app.run_registry import RunSnapshot


class ApprovalRegistry:
    def __init__(self) -> None:
        self._pending_shell: dict[str, dict] = {}
        self._pending_teach: dict[str, dict] = {}

    def add_shell(self, approval_id: str, pending: dict) -> None:
        self._pending_shell[approval_id] = dict(pending)

    def pop_shell(self, approval_id: str) -> dict | None:
        pending = self._pending_shell.pop(approval_id, None)
        return dict(pending) if pending is not None else None

    def shell_snapshot(self) -> dict[str, dict]:
        return {key: dict(value) for key, value in self._pending_shell.items()}

    def add_teach(self, teach_id: str, pending: dict) -> None:
        self._pending_teach[teach_id] = dict(pending)

    def pop_teach(self, teach_id: str) -> dict | None:
        pending = self._pending_teach.pop(teach_id, None)
        return dict(pending) if pending is not None else None

    def resume_teach(self, teach_id: str) -> bool:
        pending = self._pending_teach.get(teach_id)
        event = pending.get("event") if pending is not None else None
        if event is None:
            return False
        event.set()
        return True

    def cancel_teach(self) -> tuple[dict, ...]:
        pending = tuple(self._pending_teach.values())
        for item in pending:
            item["cancelled"] = True
            event = item.get("event")
            if event is not None:
                event.set()
        return tuple(dict(item) for item in pending)

    def teach_snapshot(self) -> dict[str, dict]:
        return {key: dict(value) for key, value in self._pending_teach.items()}

    def expire_shell_results(self) -> tuple[dict, ...]:
        stale = list(self._pending_shell.values())
        self._pending_shell.clear()
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
                *reversed(tuple(self._pending_teach.values())),
                *reversed(tuple(self._pending_shell.values())),
            ]
            if isinstance(pending.get("ui_event"), dict)
        ]
        if active is not None:
            for event in candidates:
                if event.get("run_id") == active.run_id:
                    return dict(event)
            return None
        return dict(candidates[0]) if candidates else None
