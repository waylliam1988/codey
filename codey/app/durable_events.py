"""Replay committed UI projections up to a fixed durable cursor."""

from collections.abc import Iterator

from codey.durable.model import CommitRecord
from codey.durable.session import DurableSession


def replay_commits(session: DurableSession, after: int) -> Iterator[CommitRecord]:
    # Bound replay so concurrent writers cannot keep a client in catch-up
    # forever. Subscribe before calling; later commits arrive in the live queue.
    cutoff = session.commit_sequence()
    cursor = after
    while cursor < cutoff:
        page = session.commits_after(cursor)
        if not page:
            raise RuntimeError("durable replay cursor has no corresponding commit")
        for record in page:
            if record.sequence > cutoff:
                return
            yield record
            cursor = record.sequence
