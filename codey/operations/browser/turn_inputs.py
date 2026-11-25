"""Browser inputs frozen with a provider request."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from codey.types.json_value import JsonObject, decode_json_object, require_json_boolean, require_json_string

if TYPE_CHECKING:
    from codey.operations.browser.browser_copy import CopyTurn
    from codey.operations.browser.browser_references import BrowserReferences
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.spec_model import ToolSpec


@dataclass(frozen=True)
class BrowserTurnInputs:
    copy: CopyTurn | None = None
    navigation_url: str | None = None
    source_url: str | None = None
    followups: bool = False
    fill_text: str | None = None
    references: BrowserReferences | None = None
    navigation_verifies: bool = False

    def to_payload(self) -> JsonObject:
        return {
            "browser_copy": self.copy.payload() if self.copy is not None else None,
            "task_navigation_url": self.navigation_url,
            "task_source_url": self.source_url,
            "browser_followups": self.followups,
            "browser_fill_text": self.fill_text,
            "browser_references": self.references.payload() if self.references is not None else None,
            "navigation_verifies": self.navigation_verifies,
        }

    @staticmethod
    def restore_copy(payload: JsonObject) -> CopyTurn | None:
        from codey.operations.browser.browser_copy import CopyTurn

        raw_copy = payload["browser_copy"]
        return CopyTurn.restore(decode_json_object(raw_copy)) if raw_copy is not None else None

    @classmethod
    def from_payload(
        cls, payload: JsonObject, *, copy: CopyTurn | None, overlay_compatible: bool,
        tool_names: list[str], nested_names: list[str], specs: list[ToolSpec], policy: TaskPolicy,
    ) -> BrowserTurnInputs:
        from codey.operations.browser.browser_followups import MAX_TEXT
        from codey.operations.browser.browser_references import BrowserReferences

        inventory = set(tool_names).union(nested_names)
        allows_observations = copy is None and overlay_compatible
        navigation, source = _restore_urls(payload, policy, tool_names, inventory, specs, allows_observations)
        followups = require_json_boolean(payload["browser_followups"], "browser_followups")
        fill_text = payload["browser_fill_text"]
        if fill_text is not None and (not followups or not isinstance(fill_text, str)
                                     or not fill_text.strip() or len(fill_text) > MAX_TEXT):
            raise ValueError("invalid captured browser fill text")
        raw_refs = payload["browser_references"]
        references = BrowserReferences.restore(raw_refs) if raw_refs is not None else None
        if references is not None and (not allows_observations or not any(
                spec.executor == "browser" and "ref" in dict(spec.parameters) and spec.name in nested_names
                for spec in specs)):
            raise ValueError("invalid captured browser input binding")
        verifies = require_json_boolean(payload["navigation_verifies"], "navigation_verifies")
        if verifies and (not followups or navigation is None or "browser_navigate" not in tool_names
                         or not {"browser_snapshot", "browser_read"} <= set(nested_names)):
            raise ValueError("invalid captured application navigation verification")
        return cls(copy, navigation, source, followups, fill_text, references, verifies)


def _restore_urls(
    payload: JsonObject, policy: TaskPolicy, tool_names: list[str], inventory: set[str],
    specs: list[ToolSpec], allows_observations: bool,
) -> tuple[str | None, str | None]:
    from codey.policies.network import check_fetch_url

    navigation = payload["task_navigation_url"]
    if navigation is not None:
        navigation = require_json_string(navigation, "task_navigation_url")
        targets = {"browser_navigate", "open_url"}
        spec = next((item for item in specs if item.name in targets and item.name in inventory), None)
        if (not navigation or check_fetch_url(navigation, resolve=False) is not None
                or not {"codemode", "open_url"}.intersection(tool_names)
                or spec is None or not policy.allows(spec.grant) or not allows_observations):
            raise ValueError("invalid captured task navigation binding")
    source = payload["task_source_url"]
    if source is not None:
        source = require_json_string(source, "task_source_url")
        if (not source or navigation is None or check_fetch_url(source, resolve=False) is not None
                or "open_url" not in inventory or not policy.allows("web.read")):
            raise ValueError("invalid captured task source binding")
    return navigation, source
