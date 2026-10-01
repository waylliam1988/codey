"""Work-queue cold-start hygiene: no legacy underscore aliases or dead re-exports.

Locks that the split owners expose only their formal names and that the
Store delegates through those names:

- ``work_queue_events`` has no ``_items_from_events`` alias;
- ``work_queue_sources`` has no ``_new_item`` / ``_items_from_*`` aliases;
- ``work_queue`` Store imports the formal ``items_from_events`` /
  ``new_item`` / ``items_from_*`` names and keeps no unused
  ``_apply_*`` / ``_valid_*`` / ``_WORK_TRANSITION_*`` / ``_research_proof_ref``
  re-exports for old call sites.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_events_has_no_legacy_alias() -> None:
    from codey.ghost import work_queue_events

    assert not hasattr(work_queue_events, "_items_from_events")
    text = (ROOT / "codey" / "ghost" / "work_queue_events.py").read_text(encoding="utf-8-sig")
    assert "_items_from_events = items_from_events" not in text


def test_sources_have_no_legacy_aliases() -> None:
    from codey.ghost import work_queue_sources

    for name in (
        "_new_item",
        "_items_from_continuity",
        "_items_from_research_interest_candidates",
        "_items_from_work_checkpoint",
        "_items_from_run_projection",
        "_items_from_terminal_event",
    ):
        assert not hasattr(work_queue_sources, name), name
    text = (ROOT / "codey" / "ghost" / "work_queue_sources.py").read_text(encoding="utf-8-sig")
    for alias in (
        "_new_item = new_item",
        "_items_from_continuity = items_from_continuity",
    ):
        assert alias not in text, alias


def test_store_uses_formal_owner_names() -> None:
    text = (ROOT / "codey" / "ghost" / "work_queue.py").read_text(encoding="utf-8-sig")
    for formal in (
        "items_from_events",
        "new_item",
        "items_from_continuity",
        "items_from_research_interest_candidates",
        "items_from_work_checkpoint",
        "items_from_run_projection",
        "items_from_terminal_event",
    ):
        assert formal in text, formal
    for legacy in (
        "_items_from_events",
        "_new_item",
        "_items_from_continuity",
        "_items_from_research_interest_candidates",
        "_items_from_work_checkpoint",
        "_items_from_run_projection",
        "_items_from_terminal_event",
    ):
        assert legacy not in text, legacy


def test_store_keeps_no_dead_transition_reexports() -> None:
    from codey.ghost import work_queue as store_module

    for name in (
        "_apply_claim_transition",
        "_apply_queue_transition",
        "_apply_block_transition",
        "_apply_reject_transition",
        "_valid_claim_transition",
        "_valid_release_transition",
        "_valid_work_transition",
        "_WORK_TRANSITION_PATCH_KEYS",
        "_research_proof_ref",
        "_new_item",
    ):
        assert not hasattr(store_module, name), name


def test_store_references_both_owners() -> None:
    import inspect

    from codey.ghost import work_queue as store_module
    from codey.ghost import work_queue_events as events
    from codey.ghost import work_queue_sources as sources
    from codey.ghost.work_queue import GhostWorkQueueStore

    assert callable(events.items_from_events)
    assert callable(sources.new_item)
    assert callable(sources.items_from_continuity)
    assert store_module.items_from_events is events.items_from_events
    assert store_module.items_from_continuity is sources.items_from_continuity
    # The Store must reference both owners (not just one of them).
    text = (ROOT / "codey" / "ghost" / "work_queue.py").read_text(encoding="utf-8-sig")
    assert "work_queue_events" in text
    assert "work_queue_sources" in text
    sync_src = inspect.getsource(GhostWorkQueueStore.sync_from_sources)
    assert "items_from_continuity" in sync_src
    assert "items_from_events" in sync_src
