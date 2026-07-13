"""Atomic, bounded persistence for locally recovered provider controls."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codey.local_store import read_json, write_json_atomic
from codey.provider_flow import normalize_recipe, serialize_recipe


REVIVAL_KEY = "_revival"
REVIVAL_ACTIONS = ("message_box", "send_button", "response")
MAX_CONTROL_FAILURES = 2
MAX_PROVIDER_STORE_BYTES = 64 * 1024


def complete_send(
    path: Path,
    provider_id: str,
    host: str,
    staged: dict[str, dict[str, Any]],
    verified: set[str],
    learned_verified: set[str],
    *,
    staged_flow: dict[str, tuple[str, ...]] | None = None,
    learned_flow_verified: bool = False,
    built_in_profile_hash: str = "",
) -> bool:
    """Commit staged controls together, or promote a reused provisional bundle."""
    normalized_flow = normalize_recipe(staged_flow) if staged_flow else None
    if not staged and not learned_verified and not normalized_flow and not learned_flow_verified:
        return False
    data = read_json(path, max_bytes=MAX_PROVIDER_STORE_BYTES) or {}
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
    if staged or normalized_flow:
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
        current_flow = None
        current_hash = ""
        current_meta = provider.get(REVIVAL_KEY)
        inherited_required_actions: set[str] = set()
        inherited_flow_verification = False
        if isinstance(current_meta, dict):
            current_flow = normalize_recipe(current_meta.get("flow_recipe"))
            current_hash = str(current_meta.get("built_in_profile_hash") or "")
            if current_meta.get("status") == "provisional":
                inherited_required_actions = _required_actions(current_meta)
                inherited_flow_verification = bool(
                    current_meta.get("flow_requires_verification")
                )
        flow_recipe = normalized_flow or current_flow
        provider[REVIVAL_KEY] = {
            "generation": old_generation + 1,
            "status": "provisional",
            "success_count": 1,
            "failures": 0,
            "host": host,
            "changed_actions": sorted(staged),
            "required_actions": sorted(inherited_required_actions.union(staged)),
            "verified_actions": sorted(set(staged).intersection(verified)),
            "flow_recipe": serialize_recipe(flow_recipe) if flow_recipe else None,
            "flow_changed": bool(normalized_flow),
            "flow_requires_verification": bool(
                normalized_flow or inherited_flow_verification
            ),
            "built_in_profile_hash": built_in_profile_hash or current_hash,
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
            actions = _required_actions(meta)
            has_flow = normalize_recipe(meta.get("flow_recipe")) is not None
            controls_verified = not actions or actions.issubset(learned_verified)
            flow_verified = (
                not bool(meta.get("flow_requires_verification"))
                or learned_flow_verified
            )
            fully_verified = bool(actions or has_flow) and controls_verified and flow_verified
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
        write_json_atomic(path, data, max_bytes=MAX_PROVIDER_STORE_BYTES)
    return changed


def load_flow_recipe(
    path: Path,
    provider_id: str,
    built_in_profile_hash: str,
) -> dict[str, tuple[str, ...]] | None:
    data = read_json(path, max_bytes=MAX_PROVIDER_STORE_BYTES) or {}
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        return None
    meta = provider.get(REVIVAL_KEY)
    if not isinstance(meta, dict) or meta.get("status") not in {"provisional", "active"}:
        return None
    if str(meta.get("built_in_profile_hash") or "") != built_in_profile_hash:
        return None
    return normalize_recipe(meta.get("flow_recipe"))


def record_flow_failure(path: Path, provider_id: str) -> bool:
    """Count an explicit flow mismatch and restore the previous generation."""
    data = read_json(path, max_bytes=MAX_PROVIDER_STORE_BYTES) or {}
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        return False
    meta = provider.get(REVIVAL_KEY)
    if not isinstance(meta, dict) or normalize_recipe(meta.get("flow_recipe")) is None:
        return False
    failures = int(meta.get("failures") or 0) + 1
    if failures < MAX_CONTROL_FAILURES:
        meta["failures"] = failures
    else:
        _restore_previous(provider, meta)
    write_json_atomic(path, data, max_bytes=MAX_PROVIDER_STORE_BYTES)
    return True


def record_control_failure(path: Path, provider_id: str, action: str) -> bool:
    """Count an explicit learned-control failure and restore the prior bundle."""
    data = read_json(path, max_bytes=MAX_PROVIDER_STORE_BYTES) or {}
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        return False
    record = provider.get(action)
    if not isinstance(record, dict):
        return False
    meta = provider.get(REVIVAL_KEY)
    revival_actions = _changed_actions(meta) if isinstance(meta, dict) else set()
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
    write_json_atomic(path, data, max_bytes=MAX_PROVIDER_STORE_BYTES)
    return True


def record_control_success(path: Path, provider_id: str, action: str) -> bool:
    data = read_json(path, max_bytes=MAX_PROVIDER_STORE_BYTES) or {}
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        return False
    record = provider.get(action)
    if not isinstance(record, dict):
        return False
    changed = False
    if record.get("verified") is not True:
        record["verified"] = True
        changed = True
    if int(record.get("failures") or 0):
        record["failures"] = 0
        changed = True
    if changed:
        write_json_atomic(path, data, max_bytes=MAX_PROVIDER_STORE_BYTES)
    return changed


def _restore_previous(provider: dict[str, Any], meta: dict[str, Any]) -> None:
    previous = meta.get("previous_bundle")
    previous = previous if isinstance(previous, dict) else {}
    controls = previous.get("controls")
    controls = controls if isinstance(controls, dict) else {}
    actions = _changed_actions(meta)
    for action in actions:
        record = controls.get(action)
        if isinstance(record, dict):
            provider[action] = deepcopy(record)
        else:
            provider.pop(action, None)
    old_meta = previous.get("revival")
    old_actions = _required_actions(old_meta) if isinstance(old_meta, dict) else set()
    old_has_flow = (
        isinstance(old_meta, dict)
        and normalize_recipe(old_meta.get("flow_recipe")) is not None
    )
    if (
        isinstance(old_meta, dict)
        and (old_actions or old_has_flow)
        and all(isinstance(provider.get(action), dict) for action in old_actions)
    ):
        provider[REVIVAL_KEY] = deepcopy(old_meta)
    else:
        provider.pop(REVIVAL_KEY, None)


def _changed_actions(meta: dict[str, Any]) -> set[str]:
    raw = meta.get("changed_actions", meta.get("actions", []))
    return {action for action in raw if action in REVIVAL_ACTIONS} if isinstance(
        raw, list
    ) else set()


def _required_actions(meta: dict[str, Any]) -> set[str]:
    raw = meta.get("required_actions", meta.get("actions", []))
    return {action for action in raw if action in REVIVAL_ACTIONS} if isinstance(
        raw, list
    ) else set()
