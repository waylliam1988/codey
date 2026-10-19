"""Pure, bounded public projections of durable mutations; no raw content."""

from codey.durable.model import DurableMutation, SubmissionStatus


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
    task = mutation.task
    if task is not None:
        projections.append({
            "type": "task_progressed", "task_id": task.task_id,
            "phase": task.phase, "status": task.status, "revision": task.revision,
        })
    for task in mutation.task_updates:
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
        if update.status in {
            SubmissionStatus.DONE, SubmissionStatus.UNANSWERED, SubmissionStatus.CANCELLED,
        }:
            projections.append({
                "type": "submission_completed", "submission_id": update.submission_id,
                "status": update.status, "answer_entry_id": update.answer_entry_id,
            })
    return {
        "session_id": mutation.conversation_id,
        "entry_ids": [entry.entry_id for entry in mutation.entries],
        "task_id": task.task_id if task is not None else "",
        "projections": projections,
    }
