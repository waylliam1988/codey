"""Bounded observations of existing durable facts; never completion authority."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from codey.control.protocol import ControlError, fingerprint
from codey.durable.model import thaw_json

IGNORED_DIRS = frozenset({".git", ".codey", "node_modules", ".venv", "__pycache__", ".pytest_cache",
                          ".mypy_cache", ".ruff_cache", "reference-projects", "build", "dist", ".e2e-artifacts", "artifacts"})


def project_digest(project: str) -> str:
    """Hash bounded project content, refusing symlinks and incomplete evidence."""
    digest = hashlib.sha256()
    total = 0
    count = 0
    root = Path(project).resolve()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRS and not name.startswith(".tmp-") and not Path(directory, name).is_symlink())
        for name in sorted(files):
            path = Path(directory, name)
            if path.is_symlink():
                raise ControlError("EVIDENCE_UNAVAILABLE", "Candidate contains a symbolic link")
            if name.endswith((".pyc", ".log")):
                continue
            count += 1
            size = path.stat().st_size
            total += size
            if count > 10_000 or size > 16 * 1024 * 1024 or total > 128 * 1024 * 1024:
                raise ControlError("EVIDENCE_UNAVAILABLE", "Candidate exceeds bounded digest coverage")
            before = path.stat()
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ControlError("EVIDENCE_UNAVAILABLE", "Candidate changed while being observed")
    return digest.hexdigest()


def task_for(ctx: Any, session_id: str, run_id: str) -> Any:
    try:
        task = ctx.durable.task(run_id)
    except KeyError as exc:
        raise ControlError("NOT_FOUND", "Run not found") from exc
    if task.conversation_id != session_id:
        raise ControlError("NOT_FOUND", "Run not found in this conversation")
    return task


def run_snapshot(ctx: Any, session_id: str, run_id: str, *, candidate: bool = False) -> dict[str, Any]:
    task = task_for(ctx, session_id, run_id)
    question = ctx.user_questions.pending(session_id)
    attention: list[dict[str, Any]] = []
    if question and question["run_id"] == run_id:
        attention.append({"kind": "question", **question, "audience": question.get("audience", "owner")})
    for approval in ctx.shell_approval_workflow.list_statuses(session_id=session_id):
        if approval["source_run_id"] == run_id and approval["decision"] == "pending":
            attention.append({"kind": "approval", **approval, "audience": "user"})
    entries = ctx.durable.entries(session_id)
    answers = [str(entry.payload.get("text") or "") for entry in entries
               if entry.by_task_id == run_id and entry.kind == "assistant_message"]
    status = str(task.status)
    terminal = status in {"completed", "failed", "cancelled"}
    if terminal:
        attention = []
    result: dict[str, Any] = {
        "conversation_id": session_id, "run_id": run_id, "execution": status, "phase": task.phase,
        "terminal": terminal, "cleanup_pending": any(run.run_id == run_id for run in ctx.active_runs()) and terminal,
        "answer": "\n".join(answers)[-16_000:], "answer_truncated": sum(map(len, answers)) > 16_000,
        "attention": attention, "attention_token": fingerprint(attention),
        "database_id": ctx.durable.database_id,
        "admitted_runtime": thaw_json(task.state.get("admitted_runtime")),
        "execution_runtime": thaw_json(task.state.get("execution_runtime")),
        "revision": fingerprint({"task": task.revision, "attention": attention}),
    }
    if candidate:
        project = str(task.state.get("project") or "")
        result["candidate_digest"] = project_digest(project) if project else fingerprint(answers)
        from codey.runs.ledger_projection import load_run_projection

        projection = load_run_projection(ctx.run_ledgers, session_id, run_id)
        result["verification"] = [asdict(item) for item in projection.verified_commands[-20:]] if projection else list[dict[str, Any]]()
        result["changed_files"] = list(projection.changed_files_observed[:200]) if projection else list[str]()
        result["receipt"] = asdict(projection.final_changes.receipt) if projection and projection.final_changes and projection.final_changes.receipt else None
        reviews = []
        for entry in entries:
            payload = thaw_json(entry.payload)
            if entry.kind == "control_review" and isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, dict) and data.get("run_id") == run_id:
                    reviews.append(data)
        result["reviews"] = [{**item, "stale": item["candidate_digest"] != result["candidate_digest"]} for item in reviews[-10:]]
    return result


def evidence_page(ctx: Any, params: dict[str, Any]) -> dict[str, Any]:
    session_id, run_id = params["conversation_id"], params["run_id"]
    task_for(ctx, session_id, run_id)
    offset = params.get("offset", 0)
    limit = params.get("limit", 100)
    if offset < 0 or not 1 <= limit <= 200:
        raise ControlError("INVALID_ARGUMENT", "Invalid evidence page bounds")
    if params.get("execution_id"):
        try:
            snapshot = ctx.execution_outputs.snapshot(params["execution_id"])
        except KeyError as exc:
            raise ControlError("NOT_FOUND", "Execution evidence unavailable") from exc
        if snapshot["session_id"] != session_id or snapshot["run_id"] != run_id:
            raise ControlError("NOT_FOUND", "Execution evidence unavailable")
        text = "\n".join(str(snapshot[channel]["text"]) for channel in ("stdout", "stderr"))
        return {"execution_id": params["execution_id"], "text": text[offset:offset + min(limit * 100, 16_000)],
                "complete": snapshot["execution_state"] == "settled", "total_chars": len(text),
                "execution_state": snapshot["execution_state"], "exit_code": snapshot["exit_code"],
                "truncated": any(snapshot[channel]["truncated"] for channel in ("stdout", "stderr"))}
    items: list[dict[str, Any]] = []
    for entry in ctx.durable.entries(session_id):
        if entry.by_task_id == run_id:
            items.append({"id": entry.entry_id, "kind": entry.kind, "payload": thaw_json(entry.payload)})
    selected: list[dict[str, Any]] = []
    size = 0
    for item in items[offset:offset + limit]:
        raw = json.dumps(item["payload"], ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > 8192:
            item["payload"] = {"preview_json": raw[:8192].decode(errors="ignore")}
            item["truncated"] = True
        size += len(json.dumps(item, ensure_ascii=False).encode())
        if size > 96 * 1024:
            break
        selected.append(item)
    return {"items": selected, "next_offset": offset + len(selected) if offset + len(selected) < len(items) else None}
