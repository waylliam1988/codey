"""SQLite fixtures and domain events; no emulation of the removed file stores."""

from __future__ import annotations

import json

from codey.ghost.affinity_events import replay_affinity_events
from codey.ghost.work_queue_events import items_from_events


def event_text(store):
    events = store.graph.events() if hasattr(store, "graph") else store.records.events()
    return "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events)


def decode_events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def seed_events(store, events):
    """Seed valid fixtures through the same atomic event/state boundary."""
    if hasattr(store, "graph"):
        nodes, edges = replay_affinity_events(events)
        with store.graph.transaction():
            store.graph.append(events, replace=True)
            store.graph.replace_rows((node.to_payload() for node in nodes), (edge.to_payload() for edge in edges))
    else:
        items = items_from_events(events)
        with store.records.transaction():
            store.records.append(events, replace=True)
            store.records.replace_rows(item.to_payload() for item in items)
