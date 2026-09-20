"""Pending approval state for shell and provider-control pauses."""

from __future__ import annotations

from codey.agents.shell_approval import shell_command_event_fields
from codey.app.run_registry import RunSnapshot


class ApprovalRegistry:
    def __init__(self) -> None:
        self._pending_shell: dict[str, dict] = {}
        self._pending_teach: dict[str, dict] = {}
        # Monotonic epoch: Stop (expire-all) always bumps it, even when the
        # map is already empty, so an Allow that already popped its entry can
        # still detect that a Stop landed before execution. Filtered expires
        # bump only when they actually remove entries (fail-closed, rare false
        # positives across sessions are acceptable; false negatives are not).
        self._generation = 0

    def current_generation(self) -> int:
        return self._generation

    def add_shell(self, approval_id: str, pending: dict) -> None:
        record = dict(pending)
        record["_approval_generation"] = self._generation
        self._pending_shell[approval_id] = record

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

    def expire_shell_results(
        self,
        *,
        run_id: str = "",
        exclude_run_id: str = "",
        session_id: str = "",
        output: str = "Task stopped; command approval expired.",
    ) -> tuple[dict, ...]:
        stale: list[dict] = []
        for approval_id, pending in list(self._pending_shell.items()):
            pending_run_id = str(pending.get("run_id") or "")
            if run_id and pending_run_id != run_id:
                continue
            if exclude_run_id and pending_run_id == exclude_run_id:
                continue
            if session_id and str(pending.get("session_id") or "") != session_id:
                continue
            stale.append(self._pending_shell.pop(approval_id))
        # Stop-all (no filters) always bumps, even with zero removals, to
        # invalidate an already-claimed Allow. Filtered expires bump only when
        # they remove something.
        if stale or (not run_id and not exclude_run_id and not session_id):
            self._generation += 1
        return tuple(
            {
                "type": "shell_result",
                "run_id": pending.get("run_id") or "",
                "session_id": pending.get("session_id") or "",
                "id": pending.get("id") or "",
                "approved": False,
                **shell_command_event_fields(pending),
                "cwd": pending.get("cwd") or "",
                "output": output,
                "exit_code": None,
            }
            for pending in stale
        )

    def expire_session(
        self,
        session_id: str,
        *,
        output: str = "Chat cleared; command approval expired.",
    ) -> tuple[dict, ...]:
        """Expire pending shell approvals for one session (new-chat path)."""
        return self.expire_shell_results(session_id=session_id, output=output)

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
