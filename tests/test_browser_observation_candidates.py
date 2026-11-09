"""Candidate projections use real receipt identities and preserve omitted content."""
from __future__ import annotations

import json
from dataclasses import replace

from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools.browser_observation_candidates import present_snapshot


def snapshot(text: str, generation: int, *, complete: bool = True, url: str = "https://example.com/record",
             scope: str = "a" * 32) -> ToolResult:
    observation: JsonObject = {"page_url": url, "text": text, "complete": complete, "generation": generation,
                               "elements": [{"ref": f"b{scope}g{generation}e1", "name": "Reveal", "role": "button"}],
                               "scope": "main_frame_visible_text_and_named_controls"}
    return ToolResult(ToolCall("browser_snapshot", {}), "Current snapshot", ok=True,
                      canonical={"browser_observation": observation})


def test_compact_snapshot_preserves_all_observed_text_refs_and_scope() -> None:
    session = TaskSession(TaskPolicy(frozenset({"web.read"})))
    result = snapshot("One OR two.\nException: dangerous work requires both.", 1)
    rendered = present_snapshot(session, result, delta=False)
    assert decode_json_object(json.loads(rendered.rsplit("\n", 1)[-1])) == result.canonical["browser_observation"]


def test_delta_includes_actual_baseline_text_but_only_current_action_refs() -> None:
    session = TaskSession(TaskPolicy(frozenset({"web.read"})))
    first = snapshot("Default 46 days.\nLegal hold overrides deletion.", 1)
    current = snapshot("Default 46 days.\nLegal hold overrides deletion.\nNew measured quantity: 159 grams.", 2)
    session._memory_results.update(first=first, current=current)
    before = json.dumps(current.canonical, sort_keys=True)
    rendered = present_snapshot(session, current, delta=True)
    assert "Baseline from settled receipt first" in rendered
    assert "Legal hold overrides deletion" in rendered
    assert "159 grams" in rendered
    assert "b" + "a" * 32 + "g2e1" in rendered
    assert "b" + "a" * 32 + "g1e1" not in rendered
    assert json.dumps(current.canonical, sort_keys=True) == before


def test_missing_incomplete_or_other_page_delta_base_uses_fresh_full_observation() -> None:
    for prior in (None, snapshot("Old partial content", 1, complete=False),
                  snapshot("Different page", 1, url="https://example.com/other"),
                  snapshot("Same URL in another controller", 1, scope="b" * 32)):
        session = TaskSession(TaskPolicy(frozenset({"web.read"})))
        if prior is not None:
            session._memory_results["prior"] = prior
        current = snapshot("Complete current content and exceptions", 2)
        session._memory_results["current"] = current
        rendered = present_snapshot(session, current, delta=True)
        assert "baseline unavailable" in rendered
        assert "Complete current content and exceptions" in rendered


def test_delta_without_proven_reference_scope_uses_full_current_observation() -> None:
    invalid_elements: tuple[list[JsonValue], ...] = (
        [], [{"ref": "guessed-ref"}],
        [{"ref": "b" + "a" * 32 + "g1e1"}, {"ref": "b" + "b" * 32 + "g1e2"}],
    )
    for elements in invalid_elements:
        session = TaskSession(TaskPolicy(frozenset({"web.read"})))
        prior = snapshot("An old observation cannot establish the current binding.", 1)
        observation = decode_json_object(prior.canonical["browser_observation"])
        observation["elements"] = elements
        session._memory_results["prior"] = replace(prior, canonical={"browser_observation": observation})
        current = snapshot("Complete current source with its own conditions.", 2)
        session._memory_results["current"] = current
        rendered = present_snapshot(session, current, delta=True)
        assert "baseline unavailable" in rendered
        assert "Complete current source with its own conditions" in rendered
        assert "An old observation cannot establish" not in rendered
