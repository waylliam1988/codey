"""Application control facade over Codey's single durable execution spine."""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from codey.app import control_conversations as conversations
from codey.app.control_queries import evidence_page, run_snapshot, task_for
from codey.control.protocol import COMMANDS, PROTOCOL_VERSION, Actor, ControlError, fingerprint, validate_command
from codey.durable import control
from codey.providers.catalog import DEFAULT_PROVIDER_ID, PROVIDER_LABELS


class ControlService:
    def __init__(self, ctx: Any, *, allowed_roots: tuple[Path, ...], instance_id: str = "", release_id: str = "source") -> None:
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

    def _changed(self, _result: Any, _mutation: Any) -> None:
        with self.condition:
            self.change_sequence += 1
            self.last_progress_at = time.time()
            self.condition.notify_all()

    def close(self) -> None:
        self.draining = True
        self._unsubscribe()
        with self.condition:
            self.condition.notify_all()

    def execute(self, actor: Actor, name: str, params: object) -> dict[str, Any]:
        args = validate_command(name, params)
        command = COMMANDS[name]
        if command.operator and not actor.operator:
            raise ControlError("FORBIDDEN", "This operation requires an explicit user capability")
        if name in {"prepare_release", "activate_release", "get_upgrade"}:
            from codey.launcher.releases import release_command

            return release_command(self, name, args)
        if name == "get_health":
            return self.health()
        if name == "create_conversation":
            args["conversation_id"] = "control_" + fingerprint([actor.actor_id, args["request_id"]])[:24]
        if name == "list_conversations":
            return {"conversations": [item for item in conversations.configurations(self.ctx)
                                      if actor.operator or actor.actor_id in {item["created_by"], item["controller_actor_id"]}]}
        if name == "wait_run":
            return self.wait(actor, args)
        selection = self._prepare_message(actor, args) if name == "send_message" else None
        post_cancel: list[str] = []
        gate = self.ctx._shell_spawn_gate if name in {"stop_run", "pause_control"} else contextlib.nullcontext()
        with gate, self.ctx.durable.control_line():
            if command.readonly:
                return self._read(actor, name, args)
            old = control.receipt(self.ctx.durable, args["conversation_id"], actor.actor_id, name, args)
            if old is not None:
                if name != "pause_control":
                    return old
                self._cancel_owned(old, post_cancel)
                result = old
            else:
                result, config = self._mutate(actor, name, args, post_cancel, selection)
                if name != "pause_control":
                    control.write(self.ctx.durable, args["conversation_id"], actor.actor_id, name, args, result, config=config)
        for run_id in post_cancel:
            self.ctx.request_stop(run_id)
        if name == "create_conversation":
            conversations.publish_inventory(self.ctx)
        return result

    def _read(self, actor: Actor, name: str, args: dict[str, Any]) -> dict[str, Any]:
        config = conversations.config_for(self.ctx, actor, args["conversation_id"])
        if name == "get_control":
            return config
        if name == "get_run_result":
            return run_snapshot(self.ctx, args["conversation_id"], args["run_id"], candidate=True)
        if name == "read_run_evidence":
            return evidence_page(self.ctx, args)
        if name == "read_conversation":
            session_id = args["conversation_id"]
            tasks = [task for task in self.ctx.durable.tasks() if task.conversation_id == session_id]
            return {"config": config, "history": self.ctx.durable.history_page(session_id),
                    "inbox": self.ctx.input_queue.list(session_id),
                    "runs": [run_snapshot(self.ctx, session_id, task.task_id) for task in tasks[-30:]],
                    "database_id": self.ctx.durable.database_id, "commit_seq": self.ctx.durable.commit_sequence()}
        raise ControlError("INVALID_ARGUMENT", "Unsupported query")

    def _guard(self, actor: Actor, config: dict[str, Any], args: dict[str, Any]) -> None:
        if args["control_generation"] != config["control_generation"]:
            raise ControlError("STALE_CONTROL", "Control ownership changed", details={"control_generation": config["control_generation"]})
        if not actor.operator and (config["mode"] != "delegated" or config["controller_actor_id"] != actor.actor_id):
            raise ControlError("FORBIDDEN", "This conversation is not delegated to this caller")

    def _mutate(self, actor: Actor, name: str, args: dict[str, Any], cancel: list[str], selection: Any = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if name == "create_conversation":
            config = self._create(actor, args)
            return dict(config), config
        config = conversations.config_for(self.ctx, actor, args["conversation_id"])
        if name == "send_message":
            return self._send(actor, args, config, selection), None
        self._guard(actor, config, args)
        if name == "grant_control":
            config.update(mode="delegated", controller_actor_id="codex", control_generation=config["control_generation"] + 1,
                          expires_at=time.time() + self._seconds(args), root_request_id="",
                          max_revisions=self._revisions(args))
            return dict(config), config
        if name == "pause_control":
            config.update(mode="paused", control_generation=config["control_generation"] + 1)
            # The durable generation barrier must precede cancellation cleanup.
            control.write(self.ctx.durable, args["conversation_id"], actor.actor_id, name, args, dict(config), config=config)
            self._cancel_owned(config, cancel)
            return dict(config), None
        if name == "stop_run":
            self._cancel(task_for(self.ctx, args["conversation_id"], args["run_id"]), cancel)
            return {"run_id": args["run_id"], "cancel_requested": True}, None
        if name == "answer_question":
            task_for(self.ctx, args["conversation_id"], args["run_id"])
            item = self.ctx.user_questions.get(args["question_id"])
            if not item or item["run_id"] != args["run_id"]:
                raise ControlError("NOT_FOUND", "Question does not belong to this run")
            if item.get("audience", "owner") == "user" and not actor.operator:
                raise ControlError("FORBIDDEN", "This question needs the user's decision")
            return self.ctx.user_questions.answer(item["id"], args["revision"], args["answer"]), None
        if name == "record_review":
            return self._review(actor, args), None
        raise ControlError("INVALID_ARGUMENT", "Unsupported mutation")

    @staticmethod
    def _seconds(args: dict[str, Any]) -> int:
        value = args.get("max_seconds", 1800)
        if not 1 <= value <= 86_400:
            raise ControlError("INVALID_ARGUMENT", "max_seconds must be between 1 and 86400")
        return int(value)

    @staticmethod
    def _revisions(args: dict[str, Any]) -> int:
        value = args.get("max_revisions", 3)
        if not 0 <= value <= 20:
            raise ControlError("INVALID_ARGUMENT", "max_revisions must be between 0 and 20")
        return int(value)

    def _create(self, actor: Actor, args: dict[str, Any]) -> dict[str, Any]:
        raw_project = args.get("project")
        project = Path(raw_project).expanduser().resolve() if raw_project else self.ctx.state_home / "workspaces" / args["conversation_id"]
        managed = not raw_project
        if not actor.operator and not managed and not any(project.is_relative_to(root) for root in self.allowed_roots):
            raise ControlError("FORBIDDEN", "Project is outside the service's authorized roots")
        if managed:
            project.mkdir(parents=True, exist_ok=True)
        if not project.is_dir():
            raise ControlError("INVALID_ARGUMENT", "Project directory does not exist")
        provider = args.get("provider", DEFAULT_PROVIDER_ID)
        if provider not in PROVIDER_LABELS:
            raise ControlError("INVALID_ARGUMENT", "Unknown model connection")
        return {"conversation_id": args["conversation_id"], "title": args.get("title", "Codex · Codey")[:160],
                "project": str(project), "provider": provider, "model_selection": args.get("model_selection", {}),
                "created_by": actor.actor_id, "created_at": time.time(), "mode": "delegated",
                "controller_actor_id": "codex", "control_generation": 1, "root_request_id": "",
                "expires_at": time.time() + self._seconds(args), "max_seconds": self._seconds(args),
                "max_revisions": self._revisions(args)}

    def _prepare_message(self, actor: Actor, args: dict[str, Any]) -> Any:
        from codey.providers.snapshot import capture_provider_selection

        with self.ctx.durable.control_line():
            if control.receipt(self.ctx.durable, args["conversation_id"], actor.actor_id, "send_message", args):
                return None
            identity = "control_" + fingerprint([args["conversation_id"], args["request_id"]])[:40]
            if self.ctx.input_queue.get(identity):
                return None
            config = conversations.config_for(self.ctx, actor, args["conversation_id"])
            self._guard(actor, config, args)
            if self.draining:
                raise ControlError("SERVER_DRAINING", "Service is draining; no new input was admitted")
        if not self.ctx.providers.model_preferences.allows(config["provider"]):
            raise ControlError("MODEL_DISABLED", "Selected model source is disabled; no task was admitted")
        selection = capture_provider_selection(config["provider"], config.get("model_selection") or None)
        if selection and not self.ctx.providers.model_preferences.allows(config["provider"], selection.model_id):
            raise ControlError("MODEL_DISABLED", "Selected model is disabled; no task was admitted")
        return selection

    def _send(self, actor: Actor, args: dict[str, Any], config: dict[str, Any], selection: Any) -> dict[str, Any]:
        session_id = args["conversation_id"]
        identity = "control_" + fingerprint([session_id, args["request_id"]])[:40]
        content_hash = fingerprint({"actor": actor.actor_id, "params": args})
        existing = self.ctx.input_queue.get(identity)
        if existing:
            if existing["request"].get("control_fingerprint") != content_hash:
                raise ControlError("REQUEST_CONFLICT", "This input ID already names different content")
            return self._admission(existing)
        self._guard(actor, config, args)
        if self.draining:
            raise ControlError("SERVER_DRAINING", "Service is draining; no new input was admitted")
        if not actor.operator and time.time() >= config["expires_at"]:
            raise ControlError("BUDGET_EXHAUSTED", "Delegation time budget exhausted")
        tasks = [task for task in self.ctx.durable.tasks() if task.conversation_id == session_id
                 and task.state.get("control_generation") == config["control_generation"]]
        if not actor.operator and len(tasks) >= 1 + config["max_revisions"]:
            raise ControlError("BUDGET_EXHAUSTED", "Delegation revision budget exhausted")
        mode = args.get("mode", "follow_up")
        if mode not in {"follow_up", "steer"} or not args["text"].strip():
            raise ControlError("INVALID_ARGUMENT", "Invalid input mode or empty task")
        active = self.ctx.active_run_for(session_id=session_id)
        if mode == "steer" and (active is None or active.run_id != args.get("expected_run_id")):
            raise ControlError("RUN_MISMATCH", "The expected active run is no longer current")
        if mode == "steer" and active is not None:
            target = task_for(self.ctx, session_id, active.run_id)
            if str(target.status) in {"completed", "failed", "cancelled"} or target.state.get("cancel_requested"):
                raise ControlError("RUN_MISMATCH", "The target run is already settling")
        if args.get("in_reply_to_run_id"):
            task_for(self.ctx, session_id, args["in_reply_to_run_id"])
        from codey.task.entry_auth import derive_entry_auth

        intent = args.get("intent", "auto")
        auth = derive_entry_auth({"task": args["text"], "intent": intent}, project=config["project"])
        request = {
            "session_id": session_id, "run_id": "run_" + uuid.uuid4().hex, "task": args["text"],
            "project": config["project"], "provider": config["provider"], "intent": intent,
            "max_turns": max(1, min(500, args.get("max_turns", 50))), "input_mode": mode,
            "control_generation": config["control_generation"], "controller_actor_id": actor.actor_id,
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
        return self._admission(self.ctx.input_queue.create(identity, request, model_selection=selection))

    def _admission(self, item: dict[str, Any]) -> dict[str, Any]:
        task = self.ctx.durable.task(item["request"]["run_id"])
        target = str(task.state.get("steering_target_run_id") or task.task_id)
        return {"conversation_id": task.conversation_id, "run_id": target, "input_run_id": task.task_id,
                "submission_id": task.submission_id, "entry_id": item["entry_id"] or None,
                "disposition": "queued" if item["state"] == "queued" else "placed",
                "database_id": self.ctx.durable.database_id, "commit_seq": self.ctx.durable.commit_sequence()}

    def _cancel(self, task: Any, cancel: list[str]) -> None:
        if str(task.status) not in {"completed", "failed", "cancelled"}:
            self.ctx.run_registry.request_stop(task.task_id)
            self.ctx.durable.request_cancel(task.task_id, reason="control stopped")
            cancel.append(task.task_id)

    def _cancel_owned(self, config: dict[str, Any], cancel: list[str]) -> None:
        for task in self.ctx.durable.tasks():
            if (task.conversation_id == config["conversation_id"] and task.state.get("controller_actor_id")
                    and int(task.state.get("control_generation", 0)) < config["control_generation"]):
                self._cancel(task, cancel)

    def _review(self, actor: Actor, args: dict[str, Any]) -> dict[str, Any]:
        result = run_snapshot(self.ctx, args["conversation_id"], args["run_id"], candidate=True)
        if not result["terminal"] or result["cleanup_pending"] or result["candidate_digest"] != args["candidate_digest"]:
            raise ControlError("REVISION_CONFLICT", "Review does not name the current settled candidate")
        review = {key: args[key] for key in ("run_id", "candidate_digest", "passed", "summary")}
        review.update(actor_id=actor.actor_id, source=actor.name, verification=args.get("verification", [])[:20])
        self.ctx.durable.admit_write(conversation_id=args["conversation_id"], request_id="review:" + args["request_id"],
                                     kind="control_review", data=review)
        return review

    def wait(self, actor: Actor, args: dict[str, Any]) -> dict[str, Any]:
        timeout = args.get("timeout_seconds", 30)
        until = args.get("until", "terminal_or_attention")
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
                    conversations.config_for(self.ctx, actor, args["conversation_id"])
                    snapshot = run_snapshot(self.ctx, args["conversation_id"], args["run_id"])
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

    def permits_dispatch(self, task: Any) -> bool:
        if self.draining:
            return False
        if not task.state.get("controller_actor_id"):
            return True
        with self.ctx.durable.control_line():
            config = control.latest(self.ctx.durable, task.conversation_id)
            allowed = config and config["mode"] == "delegated" and config["control_generation"] == task.state.get("control_generation")
            if not allowed or config is None or time.time() >= config["expires_at"]:
                self.ctx.durable.request_cancel(task.task_id, reason="delegation revoked or expired")
                return False
        return True

    def takeover_ui(self, session_id: str, *, paused: bool = False) -> None:
        """A real UI action revokes automation before admitting human input."""
        conversations.import_ui(self.ctx)
        cancel: list[str] = []
        with self.ctx._shell_spawn_gate, self.ctx.durable.control_line():
            config = control.latest(self.ctx.durable, session_id)
            if config is None or config["mode"] != "delegated":
                return
            config.update(mode="paused" if paused else "manual", control_generation=config["control_generation"] + 1)
            params = {"request_id": "takeover_" + str(config["control_generation"])}
            control.write(self.ctx.durable, session_id, "user", "takeover", params, dict(config), config=config)
            self._cancel_owned(config, cancel)
        for run_id in cancel:
            self.ctx.request_stop(run_id)

    def poll(self) -> None:
        if time.monotonic() - self._last_budget_check < 1 or self.draining:
            return
        self._last_budget_check = time.monotonic()
        for config in conversations.configurations(self.ctx):
            if config["mode"] == "delegated" and time.time() >= config["expires_at"]:
                with contextlib.suppress(ControlError):
                    self.execute(Actor("user", "User", True), "pause_control", {
                        "conversation_id": config["conversation_id"], "control_generation": config["control_generation"],
                        "request_id": "budget_" + str(config["control_generation"]),
                    })

    def health(self) -> dict[str, Any]:
        busy = bool(self.ctx.run_registry.active_runs())
        return {"protocol_version": PROTOCOL_VERSION, "capabilities": list(COMMANDS),
                "instance_id": self.instance_id, "release_id": self.release_id,
                "database_id": self.ctx.durable.database_id, "started_at": self.started_at,
                "status": "verifying" if self.verifying else "draining" if self.draining else "ready", "endpoint_responsive": True,
                "last_progress_at": self.last_progress_at, "suspected_stall": busy and time.time() - self.last_progress_at > 120,
                "active_runs": [{"run_id": item.run_id, "conversation_id": item.session_id} for item in self.ctx.run_registry.active_runs()]}
