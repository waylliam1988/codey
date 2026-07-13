"""Atomic, bounded persistence for locally recovered provider controls."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codey.local_store import read_json, write_json_atomic


REVIVAL_KEY = "_revival"
REVIVAL_ACTIONS = ("message_box", "send_button", "response")
MAX_CONTROL_FAILURES = 2


def complete_send(
    path: Path,
    provider_id: str,
    host: str,
    staged: dict[str, dict[str, Any]],
    verified: set[str],
    learned_verified: set[str],
) -> None:
    """Commit staged controls together, or promote a reused provisional bundle."""
    if not staged and not learned_verified:
        return
    data = read_json(path) or {}
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        provider = {}
        data[provider_id] = provider

    changed = False
    if staged:
        staged = {
            action: deepcopy(fingerprint)
            for action, fingerprint in staged.items()
            if action in REVIVAL_ACTIONS and isinstance(fingerprint, dict)
        }
    if staged:
        previous_controls = {
            action: deepcopy(provider.get(action))
            for action in staged
            if isinstance(provider.get(action), dict)
        }
        previous_meta = provider.get(REVIVAL_KEY)
        if isinstance(previous_meta, dict):
            previous_meta = {
                key: deepcopy(value)
                for key, value in previous_meta.items()
                if key != "previous_bundle"
            }
        else:
            previous_meta = None
        old_generation = int(
            (provider.get(REVIVAL_KEY) or {}).get("generation") or 0
        ) if isinstance(provider.get(REVIVAL_KEY), dict) else 0
        for action, fingerprint in staged.items():
            if action not in REVIVAL_ACTIONS:
                continue
            provider[action] = {
                "host": host,
                "fingerprint": fingerprint,
                "verified": True,
                "failures": 0,
            }
        provider[REVIVAL_KEY] = {
            "generation": old_generation + 1,
            "status": "provisional",
            "success_count": 1,
            "failures": 0,
            "host": host,
            "actions": sorted(staged),
            "verified_actions": sorted(set(staged).intersection(verified)),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "previous_bundle": {
                "controls": previous_controls,
                "revival": previous_meta,
            },
        }
        changed = True
    else:
        meta = provider.get(REVIVAL_KEY)
        if isinstance(meta, dict):
            actions = {
                action for action in meta.get("actions", [])
                if action in REVIVAL_ACTIONS
            }
            fully_verified = bool(actions) and actions.issubset(learned_verified)
            if meta.get("status") == "provisional" and fully_verified:
                meta["success_count"] = max(
                    2, int(meta.get("success_count") or 0) + 1
                )
                meta["status"] = "active"
                if int(meta.get("failures") or 0):
                    meta["failures"] = 0
                changed = True
            elif (
                meta.get("status") == "active"
                and fully_verified
                and int(meta.get("failures") or 0)
            ):
                meta["failures"] = 0
                changed = True

    for action in learned_verified:
        record = provider.get(action)
        if isinstance(record, dict):
            if record.get("verified") is not True:
                record["verified"] = True
                changed = True
            if int(record.get("failures") or 0):
                record["failures"] = 0
                changed = True
    if changed:
        write_json_atomic(path, data)


def record_control_failure(path: Path, provider_id: str, action: str) -> None:
    """Count an explicit learned-control failure and restore the prior bundle."""
    data = read_json(path) or {}
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        return
    record = provider.get(action)
    if not isinstance(record, dict):
        return
    meta = provider.get(REVIVAL_KEY)
    revival_actions = {
        item for item in meta.get("actions", []) if item in REVIVAL_ACTIONS
    } if isinstance(meta, dict) else set()
    if action in revival_actions:
        failures = int(meta.get("failures") or 0) + 1
        if failures < MAX_CONTROL_FAILURES:
            meta["failures"] = failures
            record["failures"] = failures
        else:
            _restore_previous(provider, meta)
    else:
        failures = int(record.get("failures") or 0) + 1
        if failures >= MAX_CONTROL_FAILURES:
            provider.pop(action, None)
        else:
            record["failures"] = failures
    write_json_atomic(path, data)


def record_control_success(path: Path, provider_id: str, action: str) -> None:
    data = read_json(path) or {}
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        return
    record = provider.get(action)
    if not isinstance(record, dict):
        return
    changed = False
    if record.get("verified") is not True:
        record["verified"] = True
        changed = True
    if int(record.get("failures") or 0):
        record["failures"] = 0
        changed = True
    if changed:
        write_json_atomic(path, data)


def _restore_previous(provider: dict[str, Any], meta: dict[str, Any]) -> None:
    previous = meta.get("previous_bundle")
    previous = previous if isinstance(previous, dict) else {}
    controls = previous.get("controls")
    controls = controls if isinstance(controls, dict) else {}
    actions = {
        action for action in meta.get("actions", [])
        if action in REVIVAL_ACTIONS
    }
    for action in actions:
        record = controls.get(action)
        if isinstance(record, dict):
            provider[action] = deepcopy(record)
        else:
            provider.pop(action, None)
    old_meta = previous.get("revival")
    old_actions = {
        action for action in old_meta.get("actions", [])
        if action in REVIVAL_ACTIONS
    } if isinstance(old_meta, dict) else set()
    if old_actions and all(isinstance(provider.get(action), dict) for action in old_actions):
        provider[REVIVAL_KEY] = deepcopy(old_meta)
    else:
        provider.pop(REVIVAL_KEY, None)
