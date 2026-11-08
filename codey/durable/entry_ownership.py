"""Finalize entry owners once for both raw storage and its display projection."""

from __future__ import annotations

from dataclasses import replace

from codey.durable.model import DurableMutation, EntryDraft


def finalized_entries(mutation: DurableMutation, sequence: int) -> tuple[EntryDraft, ...]:
    tasks = {
        task.submission_id: task.task_id
        for task in ((mutation.task,) if mutation.task is not None else ()) + mutation.task_updates
    }
    owners = {
        update.input_entry_id: tasks[update.submission_id]
        for update in ((mutation.submission_update,) if mutation.submission_update is not None else ())
        + mutation.submission_updates
        if update.input_entry_id is not None and update.submission_id in tasks
    }
    return tuple(replace(entry, commit_seq=sequence, by_task_id=entry.by_task_id or owners.get(entry.entry_id))
                 for entry in mutation.entries)
