"""Application control facade over Codey's single durable execution spine."""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, SupportsIndex, SupportsInt, runtime_checkable

from codey.app import control_conversations as conversations
from codey.app.control_queries import evidence_page, run_snapshot, task_for
from codey.control.protocol import COMMANDS, PROTOCOL_VERSION, Actor, ControlError, fingerprint, validate_command
from codey.durable import control
from codey.providers.catalog import DEFAULT_PROVIDER_ID, PROVIDER_LABELS
from codey.runtime.core.api_selection import ApiRunSelection
from codey.utils.object_fields import mapping_fields, object_fields, sequence_items
from codey.utils.object_fields import require_object_fields as _fields
from codey.utils.object_fields import require_object_rows as _rows

if TYPE_CHECKING:
    from codey.app.context_ports import ControlContext
    from codey.durable.model import CommitResult, DurableMutation, TaskCheckpoint


def _text(fields: Mapping[str, object], key: str, default: str | None = None) -> str:
    value = fields[key] if default is None else fields.get(key, default)
    if not isinstance(value, str):
        raise TypeError(f"control field {key} must be a string")
    return value


def _integer(fields: Mapping[str, object], key: str, default: int | None = None) -> int:
    value = fields[key] if default is None else fields.get(key, default)
    if type(value) is not int:
        raise TypeError(f"control field {key} must be an integer")
    return value


def _number(fields: Mapping[str, object], key: str, default: float | None = None) -> int | float:
    value = fields[key] if default is None else fields.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"control field {key} must be a number")
    return value


def _generation(state: Mapping[str, object]) -> int:
    value = state.get("control_generation", 0)
    if not isinstance(value, (str, bytes, bytearray, SupportsInt, SupportsIndex)):
        raise TypeError("control generation cannot be converted to an integer")
    return int(value)


def _input_identity(args: Mapping[str, object]) -> str:
    key = [_text(args, "conversation_id"), _text(args, "request_id")]
    return "control_" + fingerprint(key)[:40]


@runtime_checkable
class _HealthPort(Protocol):
    def health(self) -> object: ...


@dataclass(frozen=True)
class _ReleaseContext:
    state_home: Path


@dataclass(frozen=True)
class _ReleaseOwner:
    ctx: _ReleaseContext
    instance_id: str
    allowed_roots: tuple[Path, ...]


class ControlService:
    def __init__(self, ctx: ControlContext, *, allowed_roots: tuple[Path, ...], instance_id: str = "", release_id: str = "source") -> None:
        self.ctx = ctx
        self.allowed_roots = tuple(root.resolve() for root in allowed_roots)
        self.instance_id = instance_id or uuid.uuid4().hex
        self.release_id = release_id
        self.started_at = time.time()
        self.last_progress_at = self.started_at
        self.draining = False
        self.verifying = False
        self.condition = threading.Condition()
        self.change_sequence = 0
        self.wait_slots = threading.BoundedSemaphore(16)
        self._last_budget_check = 0.0
        self._unsubscribe = ctx.durable.subscribe_commits(self._changed)
        conversations.import_ui(ctx)
        conversations.publish_inventory(ctx)

    def _changed(self, _result: CommitResult, _mutation: DurableMutation) -> None:
        with self.condition:
            self.change_sequence += 1
            self.last_progress_at = time.time()
            self.condition.notify_all()

    def close(self) -> None:
        self.draining = True
        self._unsubscribe()
        with self.condition:
            self.condition.notify_all()

    def execute(self, actor: Actor, name: str, params: object) -> dict[str, object]:
        raw_args: object = validate_command(name, params)
        args = _fields(raw_args)
        command = COMMANDS[name]
        if command.operator and not actor.operator:
            raise ControlError("FORBIDDEN", "This operation requires an explicit user capability")
        if name in {"prepare_release", "activate_release", "get_upgrade"}:
            from codey.launcher.releases import release_command

            home = self.ctx.state_home
            if home is None:
                raise TypeError("release commands require persistent state")
            owner = _ReleaseOwner(_ReleaseContext(home), self.instance_id, self.allowed_roots)
            response: object = release_command(owner, name, args)
            return _fields(response)
        if name == "get_health":
            return self.health()
        if name == "resume_scheduler":
            self.ctx.durable_scheduler.resume()
            scheduler_health = self.health().get("scheduler")
            if not object_fields(scheduler_health):
                raise ControlError("INTERNAL_ERROR", "Scheduler health is unavailable")
            return scheduler_health
        if name == "create_conversation":
            create_key = [actor.actor_id, _text(args, "request_id")]
            args["conversation_id"] = "control_" + fingerprint(create_key)[:24]
        if name == "list_conversations":
            return {"conversations": [item for item in self._configurations()
                                      if actor.operator or actor.actor_id in {_text(item, "created_by"), _text(item, "controller_actor_id")}]}
        if name == "wait_run":
            return self.wait(actor, args)
        selection = self._prepare_message(actor, args) if name == "send_message" else None
        post_cancel: list[str] = []
        gate = self.ctx.task_runtime.spawn_line() if name in {"stop_run", "pause_control"} else contextlib.nullcontext()
        with gate, self.ctx.durable.control_line():
            if command.readonly:
                return self._read(actor, name, args)
            old_value: object = control.receipt(self.ctx.durable, _text(args, "conversation_id"), actor.actor_id, name, args)
            old = _fields(old_value) if old_value is not None else None
            if old is not None:
                if name != "pause_control":
                    return old
                self._cancel_owned(old, post_cancel)
                result = old
            else:
                result, config = self._mutate(actor, name, args, post_cancel, selection)
                if name != "pause_control":
                    control.write(self.ctx.durable, _text(args, "conversation_id"), actor.actor_id, name, args, result, config=config)
        for run_id in post_cancel:
            self.ctx.request_stop(run_id)
        if name == "create_conversation":
            conversations.publish_inventory(self.ctx)
        return result

    def _config_for(self, actor: Actor, session_id: str) -> dict[str, object]:
        config: object = conversations.config_for(self.ctx, actor, session_id)
        return _fields(config)

    def _configurations(self) -> list[dict[str, object]]:
        configs: object = conversations.configurations(self.ctx)
        return _rows(configs)

    def _latest(self, session_id: str) -> dict[str, object] | None:
        config: object = control.latest(self.ctx.durable, session_id)
        return _fields(config) if config is not None else None

    def _read(self, actor: Actor, name: str, args: dict[str, object]) -> dict[str, object]:
        config = self._config_for(actor, _text(args, "conversation_id"))
        if name == "get_control":
            return config
        if name == "get_run_result":
            return run_snapshot(self.ctx, _text(args, "conversation_id"), _text(args, "run_id"), candidate=True)
        if name == "read_run_evidence":
            return evidence_page(self.ctx, args)
        if name == "read_conversation":
            session_id = _text(args, "conversation_id")
            tasks = [task for task in self.ctx.durable.tasks() if task.conversation_id == session_id]
            history: object = self.ctx.durable.history_page(session_id)
            inbox: object = self.ctx.input_queue.list(session_id)
            return {"config": config, "history": _fields(history),
                    "inbox": _rows(inbox),
                    "runs": [run_snapshot(self.ctx, session_id, task.task_id) for task in tasks[-30:]],
                    "database_id": self.ctx.durable.database_id, "commit_seq": self.ctx.durable.commit_sequence()}
        raise ControlError("INVALID_ARGUMENT", "Unsupported query")

    def _guard(self, actor: Actor, config: dict[str, object], args: dict[str, object]) -> None:
        if _integer(args, "control_generation") != _integer(config, "control_generation"):
            details: dict[str, object] = {"control_generation": _integer(config, "control_generation")}
            raise ControlError("STALE_CONTROL", "Control ownership changed", details=details)
        if not actor.operator and (_text(config, "mode") != "delegated" or _text(config, "controller_actor_id") != actor.actor_id):
            raise ControlError("FORBIDDEN", "This conversation is not delegated to this caller")

    def _mutate(self, actor: Actor, name: str, args: dict[str, object], cancel: list[str], selection: ApiRunSelection | None = None) -> tuple[dict[str, object], dict[str, object] | None]:
        if name == "create_conversation":
            config = self._create(actor, args)
            return dict(config), config
        config = self._config_for(actor, _text(args, "conversation_id"))
        if name == "send_message":
            return self._send(actor, args, config, selection), None
        self._guard(actor, config, args)
        if name == "grant_control":
            config.update(mode="delegated", controller_actor_id="codex", control_generation=_integer(config, "control_generation") + 1,
                          expires_at=time.time() + self._seconds(args), root_request_id="",
                          max_revisions=self._revisions(args))
            return dict(config), config
        if name == "pause_control":
            config.update(mode="paused", control_generation=_integer(config, "control_generation") + 1)
            # The durable generation barrier must precede cancellation cleanup.
            control.write(self.ctx.durable, _text(args, "conversation_id"), actor.actor_id, name, args, dict[str, object](config), config=config)
            self._cancel_owned(config, cancel)
            return dict(config), None
        if name == "stop_run":
            self._cancel(task_for(self.ctx, _text(args, "conversation_id"), _text(args, "run_id")), cancel)
            return {"run_id": _text(args, "run_id"), "cancel_requested": True}, None
        if name == "answer_question":
            task_for(self.ctx, _text(args, "conversation_id"), _text(args, "run_id"))
            question: object = self.ctx.user_questions.get(_text(args, "question_id"))
            if not question:
                raise ControlError("NOT_FOUND", "Question does not belong to this run")
            item = _fields(question)
            if item["run_id"] != _text(args, "run_id"):
                raise ControlError("NOT_FOUND", "Question does not belong to this run")
            if item.get("audience", "owner") == "user" and not actor.operator:
                raise ControlError("FORBIDDEN", "This question needs the user's decision")
            answer: object = self.ctx.user_questions.answer(_text(item, "id"), _integer(args, "revision"), _text(args, "answer"))
            return _fields(answer), None
        if name == "record_review":
            return self._review(actor, args), None
        raise ControlError("INVALID_ARGUMENT", "Unsupported mutation")

    @staticmethod
    def _seconds(args: dict[str, object]) -> int:
        value = _integer(args, "max_seconds", 1800)
        if not 1 <= value <= 86_400:
            raise ControlError("INVALID_ARGUMENT", "max_seconds must be between 1 and 86400")
        return value

    @staticmethod
    def _revisions(args: dict[str, object]) -> int:
        value = _integer(args, "max_revisions", 3)
        if not 0 <= value <= 20:
            raise ControlError("INVALID_ARGUMENT", "max_revisions must be between 0 and 20")
        return value

    def _create(self, actor: Actor, args: dict[str, object]) -> dict[str, object]:
        raw_project = _text(args, "project", "")
        if raw_project:
            project = Path(raw_project).expanduser().resolve()
        else:
            if self.ctx.state_home is None:
                raise TypeError("managed control workspaces require persistent state")
            project = self.ctx.state_home / "workspaces" / _text(args, "conversation_id")
        managed = not raw_project
        if not actor.operator and not managed and not any(project.is_relative_to(root) for root in self.allowed_roots):
            raise ControlError("FORBIDDEN", "Project is outside the service's authorized roots")
        if managed:
            project.mkdir(parents=True, exist_ok=True)
        if not project.is_dir():
            raise ControlError("INVALID_ARGUMENT", "Project directory does not exist")
        provider = _text(args, "provider", DEFAULT_PROVIDER_ID)
        if provider not in PROVIDER_LABELS:
            raise ControlError("INVALID_ARGUMENT", "Unknown model connection")
        return {"conversation_id": _text(args, "conversation_id"), "title": _text(args, "title", "Codex · Codey")[:160],
                "project": str(project), "provider": provider, "model_selection": args.get("model_selection", {}),
                "created_by": actor.actor_id, "created_at": time.time(), "mode": "delegated",
                "controller_actor_id": "codex", "control_generation": 1, "root_request_id": "",
                "expires_at": time.time() + self._seconds(args), "max_seconds": self._seconds(args),
                "max_revisions": self._revisions(args),
                **({"allow_web": args["allow_web"]} if "allow_web" in args else {})}

    def _prepare_message(self, actor: Actor, args: dict[str, object]) -> ApiRunSelection | None:
        from codey.providers.snapshot import capture_provider_selection

        with self.ctx.durable.control_line():
            previous: object = control.receipt(self.ctx.durable, _text(args, "conversation_id"), actor.actor_id, "send_message", args)
            if previous:
                return None
            identity = _input_identity(args)
            queued: object = self.ctx.input_queue.get(identity)
            if queued:
                return None
            config = self._config_for(actor, _text(args, "conversation_id"))
            self._guard(actor, config, args)
            if self.draining:
                raise ControlError("SERVER_DRAINING", "Service is draining; no new input was admitted")
        if not self.ctx.providers.model_preferences.allows(_text(config, "provider")):
            raise ControlError("MODEL_DISABLED", "Selected model source is disabled; no task was admitted")
        raw_selection = config.get("model_selection") or None
        if raw_selection is not None and not isinstance(raw_selection, ApiRunSelection) and not mapping_fields(raw_selection):
            raise TypeError("control model selection must be an object")
        selection = capture_provider_selection(_text(config, "provider"), raw_selection)
        if selection and not self.ctx.providers.model_preferences.allows(_text(config, "provider"), selection.model_id):
            raise ControlError("MODEL_DISABLED", "Selected model is disabled; no task was admitted")
        return selection

    def _send(self, actor: Actor, args: dict[str, object], config: dict[str, object], selection: ApiRunSelection | None) -> dict[str, object]:
        session_id = _text(args, "conversation_id")
        identity = _input_identity(args)
        fingerprint_fields: dict[str, object] = {"actor": actor.actor_id, "params": args}
        content_hash = fingerprint(fingerprint_fields)
        existing_value: object = self.ctx.input_queue.get(identity)
        if existing_value:
            existing = _fields(existing_value)
            if _fields(existing["request"]).get("control_fingerprint") != content_hash:
                raise ControlError("REQUEST_CONFLICT", "This input ID already names different content")
            return self._admission(existing)
        self._guard(actor, config, args)
        if self.draining:
            raise ControlError("SERVER_DRAINING", "Service is draining; no new input was admitted")
        if not actor.operator and time.time() >= _number(config, "expires_at"):
            raise ControlError("BUDGET_EXHAUSTED", "Delegation time budget exhausted")
        tasks = [task for task in self.ctx.durable.tasks() if task.conversation_id == session_id
                 and task.state.get("control_generation") == _integer(config, "control_generation")]
        if not actor.operator and len(tasks) >= 1 + _integer(config, "max_revisions"):
            raise ControlError("BUDGET_EXHAUSTED", "Delegation revision budget exhausted")
        mode = _text(args, "mode", "follow_up")
        if mode not in {"follow_up", "steer"} or not _text(args, "text").strip():
            raise ControlError("INVALID_ARGUMENT", "Invalid input mode or empty task")
        active = self.ctx.active_run_for(session_id=session_id)
        if mode == "steer" and (active is None or active.run_id != args.get("expected_run_id")):
            raise ControlError("RUN_MISMATCH", "The expected active run is no longer current")
        if mode == "steer" and active is not None:
            target = task_for(self.ctx, session_id, active.run_id)
            if str(target.status) in {"completed", "failed", "cancelled"} or target.state.get("cancel_requested"):
                raise ControlError("RUN_MISMATCH", "The target run is already settling")
        if args.get("in_reply_to_run_id"):
            task_for(self.ctx, session_id, _text(args, "in_reply_to_run_id"))
        from codey.task.entry_auth import derive_entry_auth

        intent = _text(args, "intent", "auto")
        auth = derive_entry_auth({**config, **args, "task": _text(args, "text"), "intent": intent}, project=_text(config, "project"))
        request: dict[str, object] = {
            "session_id": session_id, "run_id": "run_" + uuid.uuid4().hex, "task": _text(args, "text"),
            "project": _text(config, "project"), "provider": _text(config, "provider"), "intent": intent,
            "max_turns": max(1, min(500, _integer(args, "max_turns", 50))), "input_mode": mode,
            "control_generation": _integer(config, "control_generation"), "controller_actor_id": actor.actor_id,
            "steering_target_run_id": active.run_id if mode == "steer" and active else "",
            "admitted_runtime": {"instance_id": self.instance_id, "release_id": self.release_id},
            "control_fingerprint": content_hash, "requested_capabilities": list(auth.requested_capabilities),
            "denied_capabilities": list(auth.denied_capabilities), "strict_research": auth.strict_research,
            "sources_open_required": auth.sources_open_required, "project_changes_required": auth.project_changes_required,
            "message_metadata": {"source": {"actor_id": actor.actor_id, "name": actor.name, "kind": "external"},
                                 "in_reply_to_run_id": args.get("in_reply_to_run_id", "")},
        }
        if config.get("model_selection"):
            request["model_selection"] = config["model_selection"]
        queued: object = self.ctx.input_queue.create(identity, request, model_selection=selection)
        return self._admission(_fields(queued))

    def _admission(self, item: dict[str, object]) -> dict[str, object]:
        task = self.ctx.durable.task(_text(_fields(item["request"]), "run_id"))
        entry_id = item["entry_id"]
        if entry_id is not None and not isinstance(entry_id, str):
            raise TypeError("queued entry ID must be a string")
        target = str(task.state.get("steering_target_run_id") or task.task_id)
        return {"conversation_id": task.conversation_id, "run_id": target, "input_run_id": task.task_id,
                "submission_id": task.submission_id, "entry_id": entry_id or None,
                "disposition": "queued" if item["state"] == "queued" else "placed",
                "database_id": self.ctx.durable.database_id, "commit_seq": self.ctx.durable.commit_sequence()}

    def _cancel(self, task: TaskCheckpoint, cancel: list[str]) -> None:
        if str(task.status) not in {"completed", "failed", "cancelled"}:
            self.ctx.run_registry.request_stop(task.task_id)
            self.ctx.durable.request_cancel(task.task_id, reason="control stopped")
            cancel.append(task.task_id)

    def _cancel_owned(self, config: dict[str, object], cancel: list[str]) -> None:
        for task in self.ctx.durable.tasks():
            if (task.conversation_id == _text(config, "conversation_id") and task.state.get("controller_actor_id")
                    and _generation(task.state) < _integer(config, "control_generation")):
                self._cancel(task, cancel)

    def _review(self, actor: Actor, args: dict[str, object]) -> dict[str, object]:
        result = run_snapshot(self.ctx, _text(args, "conversation_id"), _text(args, "run_id"), candidate=True)
        if not result["terminal"] or result["cleanup_pending"] or result["candidate_digest"] != args["candidate_digest"]:
            raise ControlError("REVISION_CONFLICT", "Review does not name the current settled candidate")
        review = {key: args[key] for key in ("run_id", "candidate_digest", "passed", "summary")}
        verification = args.get("verification", ())
        if not sequence_items(verification):
            raise TypeError("review verification must be a sequence")
        review.update(actor_id=actor.actor_id, source=actor.name, verification=list(verification[:20]))
        self.ctx.durable.admit_write(conversation_id=_text(args, "conversation_id"), request_id="review:" + _text(args, "request_id"),
                                     kind="control_review", data=review)
        return review

    def wait(self, actor: Actor, args: dict[str, object]) -> dict[str, object]:
        timeout = _number(args, "timeout_seconds", 30)
        until = _text(args, "until", "terminal_or_attention")
        if not 0 <= timeout <= 30 or until not in {"change", "terminal_or_attention"}:
            raise ControlError("INVALID_ARGUMENT", "Invalid wait bounds or condition")
        if not self.wait_slots.acquire(blocking=False):
            raise ControlError("RATE_LIMITED", "Too many concurrent observers")
        deadline = time.monotonic() + timeout
        try:
            while True:
                with self.condition:
                    sequence = self.change_sequence
                with self.ctx.durable.control_line():
                    self._config_for(actor, _text(args, "conversation_id"))
                    snapshot = run_snapshot(self.ctx, _text(args, "conversation_id"), _text(args, "run_id"))
                reason = "terminal" if snapshot["terminal"] else ""
                if snapshot["attention"] and snapshot["attention_token"] != args.get("seen_attention_token"):
                    reason = reason or "attention"
                if until == "change" and snapshot["revision"] != args.get("after_revision"):
                    reason = reason or "changed"
                remaining = deadline - time.monotonic()
                if reason or remaining <= 0 or self.draining:
                    return {"reason": reason or "timeout", "snapshot": snapshot}
                with self.condition:
                    if sequence == self.change_sequence:
                        self.condition.wait(remaining)
        finally:
            self.wait_slots.release()

    def permits_dispatch(self, task: TaskCheckpoint) -> bool:
        if self.draining:
            return False
        if not task.state.get("controller_actor_id"):
            return True
        with self.ctx.durable.control_line():
            config = self._latest(task.conversation_id)
            allowed = config and _text(config, "mode") == "delegated" and _integer(config, "control_generation") == task.state.get("control_generation")
            if not allowed or config is None or time.time() >= _number(config, "expires_at"):
                self.ctx.durable.request_cancel(task.task_id, reason="delegation revoked or expired")
                return False
        return True

    def takeover_ui(self, session_id: str, *, paused: bool = False) -> None:
        """A real UI action revokes automation before admitting human input."""
        conversations.import_ui(self.ctx)
        cancel: list[str] = []
        with self.ctx.task_runtime.spawn_line(), self.ctx.durable.control_line():
            config = self._latest(session_id)
            if config is None or _text(config, "mode") != "delegated":
                return
            config.update(mode="paused" if paused else "manual", control_generation=_integer(config, "control_generation") + 1)
            params: dict[str, object] = {"request_id": "takeover_" + str(_integer(config, "control_generation"))}
            control.write(self.ctx.durable, session_id, "user", "takeover", params, dict[str, object](config), config=config)
            self._cancel_owned(config, cancel)
        for run_id in cancel:
            self.ctx.request_stop(run_id)

    def poll(self) -> None:
        if time.monotonic() - self._last_budget_check < 1 or self.draining:
            return
        self._last_budget_check = time.monotonic()
        for config in self._configurations():
            if _text(config, "mode") == "delegated" and time.time() >= _number(config, "expires_at"):
                with contextlib.suppress(ControlError):
                    self.execute(Actor("user", "User", True), "pause_control", {
                        "conversation_id": _text(config, "conversation_id"), "control_generation": _integer(config, "control_generation"),
                        "request_id": "budget_" + str(_integer(config, "control_generation")),
                    })

    def health(self) -> dict[str, object]:
        busy = bool(self.ctx.run_registry.active_runs())
        scheduler: object = getattr(self.ctx, "durable_scheduler", None)
        scheduler_health: dict[str, object] | None = None
        if scheduler is not None:
            if not isinstance(scheduler, _HealthPort):
                raise TypeError("scheduler must expose health")
            health = scheduler.health()
            scheduler_health = _fields(health) if health is not None else None
        scheduler_degraded = (
            isinstance(scheduler_health, dict)
            and scheduler_health.get("enabled") is False
            and scheduler_health.get("loop_failure") is not None
        )
        payload: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": list(COMMANDS),
            "instance_id": self.instance_id,
            "release_id": self.release_id,
            "database_id": self.ctx.durable.database_id,
            "started_at": self.started_at,
            "status": "verifying" if self.verifying else "draining" if self.draining else "degraded" if scheduler_degraded else "ready",
            "endpoint_responsive": True,
            "last_progress_at": self.last_progress_at,
            "suspected_stall": busy and time.time() - self.last_progress_at > 120,
            "active_runs": [
                {"run_id": item.run_id, "conversation_id": item.session_id}
                for item in self.ctx.run_registry.active_runs()
            ],
        }
        if scheduler is not None:
            payload["scheduler"] = scheduler_health
        return payload
