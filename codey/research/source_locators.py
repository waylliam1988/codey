"""One codec and identity rule for captured source locators, live and restored."""
from __future__ import annotations

import re
from collections.abc import Mapping

from codey.research.ledger_receipts import source_observation
from codey.research.receipt_types import SourceSearchObservation
from codey.research.source_search import SourceSearchHit, render_results
from codey.runtime.core.execution_record import HitTarget


def _identity(target: HitTarget) -> tuple[str, str, int, str]:
    return target["url"], target["text_hash"], target["offset"], target["pages"]


def observation_targets(observation: SourceSearchObservation) -> tuple[HitTarget, ...]:
    source, _, _ = source_observation(observation["source_snapshot"])
    return tuple(HitTarget(url=source.final_url, text_hash=source.text_hash, offset=hit["offset"],
                           pages=str(hit["page"]) if hit["page"] is not None else "")
                 for hit in observation["hits"])


def checked_hit_targets(value: object, existing: Mapping[str, HitTarget], *,
                        observation: SourceSearchObservation | None = None) -> dict[str, HitTarget]:
    """Validate the entire receipt before the caller mutates a projection."""
    if not isinstance(value, dict):
        raise ValueError("hit_targets must be a mapping")
    pending: dict[str, HitTarget] = {}
    for hid, target in value.items():
        if type(hid) is not str or not hid:
            raise ValueError("hit id must be a non-empty string")
        if not isinstance(target, dict):
            raise ValueError("hit target must be a mapping")
        url = target.get("url", "")
        if type(url) is not str:
            raise ValueError("hit target url must be a string")
        text_hash = target.get("text_hash")
        if type(text_hash) is not str or re.fullmatch(r"[0-9a-f]{16}", text_hash) is None:
            raise ValueError("hit target requires a captured source text hash")
        offset = target.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("hit target offset must be a nonnegative integer")
        pages = target.get("pages", "")
        if type(pages) is not str:
            raise ValueError("hit target pages must be a string")
        if not url.strip():
            raise ValueError("hit target is malformed")
        if observation is not None and url != url.strip():
            raise ValueError("hit targets differ from the captured source search observation")
        clean = HitTarget(url=url.strip(), text_hash=text_hash, offset=offset, pages=pages)
        if hid in existing and existing[hid] != clean:
            raise ValueError(f"conflicting hit target for {hid}")
        pending[hid] = clean
    if observation is not None:
        expected = {_identity(target) for target in observation_targets(observation)}
        actual = {_identity(target) for target in pending.values()}
        if actual != expected or len(pending) != len(actual):
            raise ValueError("hit targets differ from the captured source search observation")
    return pending


def build_hit_mapping(observation: SourceSearchObservation, existing: Mapping[str, HitTarget]) -> tuple[str, dict[str, HitTarget]]:
    """Reuse exact captured targets; allocate query-local labels without mutation."""
    batch: dict[str, HitTarget] = {}
    by_target = {_identity(target): key for key, target in existing.items()}
    hit_ids: list[str] = []
    counter = len(existing) + 1
    for target in observation_targets(observation):
        identity = _identity(target)
        hit_id = by_target.get(identity, "")
        if not hit_id:
            while f"h{counter}" in existing or f"h{counter}" in batch:
                counter += 1
            hit_id = f"h{counter}"
            counter += 1
            by_target[identity] = hit_id
        batch[hit_id] = target
        hit_ids.append(hit_id)
    hits = [SourceSearchHit(**hit) for hit in observation["hits"]]
    return render_results(hits, hit_ids=tuple(hit_ids)), batch
