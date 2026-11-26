"""Persisted protocol versions remain strict integers across the decoder refactor."""
from __future__ import annotations

import pytest

from codey.knowledge.model import canonical_json
from codey.operations.browser.browser_references import CONTROL_PRESENTATION_VERSION, BrowserReferences
from codey.operations.provider_receipts import TURN_SNAPSHOT_SCHEMA, restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.types.json_value import JsonObject, JsonValue, parse_json_object


def snapshot_payload() -> JsonObject:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})))
    return snapshot_description(build_turn_snapshot(session))


def control_payload() -> JsonObject:
    return BrowserReferences("snapshot", '{"target_id":"tab","frame_id":"main","loader_id":"document"}', 7, ()).payload()


def test_current_input_contract_roundtrips_frozen_declarations() -> None:
    original = snapshot_payload()
    restored = restore_snapshot(parse_json_object(canonical_json(original)))
    assert original["schema_version"] == TURN_SNAPSHOT_SCHEMA
    assert snapshot_description(restored) == original


@pytest.mark.parametrize("version", [True, False, 11.0, 14.0, "14", None, [], {}])
def test_input_version_rejects_nonintegers_without_coercion(version: JsonValue) -> None:
    payload = snapshot_payload()
    payload["schema_version"] = version
    with pytest.raises(ValueError, match="snapshot version must be an integer"):
        restore_snapshot(payload)


@pytest.mark.parametrize("version", [-1, 0, *range(10, TURN_SNAPSHOT_SCHEMA), TURN_SNAPSHOT_SCHEMA + 1])
def test_unsupported_input_version_is_rejected_before_shape_validation(version: int) -> None:
    with pytest.raises(ValueError, match="unsupported captured turn snapshot version; start a new request"):
        restore_snapshot({"schema_version": version})


def test_supported_input_version_still_requires_the_original_shape() -> None:
    with pytest.raises(ValueError, match="invalid captured turn snapshot shape"):
        restore_snapshot({"schema_version": TURN_SNAPSHOT_SCHEMA})


def test_current_control_presentation_roundtrips() -> None:
    payload = control_payload()
    assert payload["presentation_version"] == CONTROL_PRESENTATION_VERSION
    assert BrowserReferences.restore(parse_json_object(canonical_json(payload))).payload() == payload


def test_missing_control_version_is_rejected() -> None:
    payload = control_payload()
    del payload["presentation_version"]
    with pytest.raises(ValueError, match="control binding"):
        BrowserReferences.restore(payload)


@pytest.mark.parametrize("version", [True, False, 1.0, 4.0, "4", None, [], {}])
def test_control_version_rejects_nonintegers_without_coercion(version: JsonValue) -> None:
    payload = control_payload()
    payload["presentation_version"] = version
    with pytest.raises(ValueError, match="control presentation version must be an integer"):
        BrowserReferences.restore(payload)


@pytest.mark.parametrize("version", [-1, 0, 1, 2, 3, 5])
def test_unknown_control_version_remains_an_error(version: int) -> None:
    payload = control_payload()
    payload["presentation_version"] = version
    with pytest.raises(ValueError, match="Unsupported frozen browser control presentation version"):
        BrowserReferences.restore(payload)
