"""Pure, bounded public projections of durable mutations; no raw content."""

from collections.abc import Mapping, Sequence

from codey.durable.input_receipts import receipt_fields
from codey.durable.model import CommitResult, DurableMutation, SubmissionStatus


def committed_task_version(result: CommitResult, task_id: str) -> dict[str, object]:
    """The exact task version in this commit, not its conversation revision."""
    if result.record is None:
        return {}
    projections = result.record.payload.get("projections", ())
    if not isinstance(projections, (tuple, list)):
        raise ValueError("commit projections must be a sequence")
    rows: Sequence[object] = projections
    for row in rows:
        if isinstance(row, Mapping):
            fields: Mapping[object, object] = row
            if fields.get("type") == "task_progressed" and fields.get("task_id") == task_id:
                return {"task_revision": fields["revision"], "task_status": str(fields["status"])}
    return {}


def mutation_projection(mutation: DurableMutation) -> dict[str, object]:
    projections: list[dict[str, object]] = []
    creation = mutation.create_conversation
    if creation is not None:
        projections.append({
            "type": "conversation_created",
            "conversation_id": mutation.conversation_id,
            "parent_conversation_id": creation.parent_conversation_id,
            "parent_entry_id": creation.parent_entry_id,
        })
    if mutation.delete_conversation is not None:
        projections.append({
            "type": "conversation_deleted",
            "conversation_id": mutation.conversation_id,
        })
    if mutation.branch_entry_id is not None:
        projections.append({
            "type": "conversation_branched",
            "conversation_id": mutation.conversation_id,
            "head_entry_id": mutation.branch_entry_id,
        })
    projections.extend([
        {"type": "entry_appended", "entry_id": entry.entry_id, "kind": entry.kind}
        for entry in mutation.entries
    ])
    submission = mutation.submission
    if submission is not None:
        projections.append({
            "type": "submission_admitted",
            "submission_id": submission.submission_id,
            "request_id": submission.request_id,
            "status": submission.status,
            "placement": submission.placement,
            "mode": submission.mode,
            "input_receipts": receipt_fields(submission.content),
        })
    primary_task = mutation.task
    tasks = ((primary_task,) if primary_task is not None else ()) + mutation.task_updates
    for task in tasks:
        projections.append({
            "type": "task_progressed", "task_id": task.task_id,
            "phase": task.phase, "status": task.status, "revision": task.revision,
        })
    for effect in ((mutation.effect,) if mutation.effect is not None else ()) + mutation.effects + mutation.settlements:
        if effect.settlement is not None:
            projections.append({
                "type": "effect_settled", "task_id": effect.task_id,
                "effect_id": effect.effect_id, "status": effect.status,
            })
    update = mutation.submission_update
    updates = ((update,) if update is not None else ()) + mutation.submission_updates
    for update in updates:
        projections.append({
            "type": "submission_progressed",
            "submission_id": update.submission_id,
            "status": update.status,
            "placement": update.placement,
        })
        if update.status in {
            SubmissionStatus.DONE, SubmissionStatus.UNANSWERED, SubmissionStatus.CANCELLED,
        }:
            projections.append({
                "type": "submission_completed", "submission_id": update.submission_id,
                "status": update.status, "answer_entry_id": update.answer_entry_id,
            })
    return {
        "session_id": mutation.conversation_id,
        **({"edited_entry_id": mutation.edited_entry_id, "replaced_run_ids": list(mutation.replaced_run_ids)}
           if mutation.edited_entry_id is not None else {}),
        "entry_ids": [entry.entry_id for entry in mutation.entries],
        "task_id": primary_task.task_id if primary_task is not None else "",
        "projections": projections,
    }
