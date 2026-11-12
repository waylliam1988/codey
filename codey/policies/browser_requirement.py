"""Explicit browser prerequisites carried by a frozen task policy."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from codey.policies.network import check_fetch_url
from codey.types.json_value import decode_json_object, require_json_string

RESEARCH_PREFIX = "browser_research_sequence:"


@dataclass(frozen=True)
class BrowserResearchSequence:
    url: str
    condition: str
    value: str

    def __post_init__(self) -> None:
        if (not self.url or len(self.url) > 2048 or self.url != self.url.strip()
                or check_fetch_url(self.url, resolve=False)):
            raise ValueError("browser sequence requires an eligible, explicit page URL")
        from codey.toolchain.tool_spec import validate_args_against_spec

        error = validate_args_against_spec("browser_wait", {"condition": self.condition, "value": self.value, "timeout_ms": 100})
        if error or not self.value.strip() or len(self.value) > 512:
            raise ValueError(error or "browser sequence requires a bounded, nonempty wait value")

    @property
    def declaration(self) -> str:
        return RESEARCH_PREFIX + json.dumps({"url": self.url, "condition": self.condition, "value": self.value},
                                            ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def check_id(self) -> str:
        return RESEARCH_PREFIX + hashlib.sha256(self.declaration.encode()).hexdigest()[:24]

    @property
    def receipt_tools(self) -> frozenset[str]:
        return frozenset({"browser_wait", "browser_read"})

    @property
    def guidance(self) -> str:
        return (f"Required browser research evidence for {self.url}: on that page, {self.wait_step} "
                "Then use browser_read on the same document and cite its evidence in submit_research_answer.")

    @property
    def wait_step(self) -> str:
        return f"Use browser_wait with condition={self.condition!r} and value={self.value!r}."


def require_browser_research_sequence(*, url: str, condition: str, value: str) -> str:
    return BrowserResearchSequence(url, condition, value).declaration


def parse_browser_requirement(declaration: str) -> BrowserResearchSequence | None:
    if not declaration.startswith(RESEARCH_PREFIX):
        return None
    row = decode_json_object(json.loads(declaration[len(RESEARCH_PREFIX):]))
    if set(row) != {"url", "condition", "value"}:
        raise ValueError("invalid browser research sequence fields")
    rule = BrowserResearchSequence(*(require_json_string(row[key], key) for key in ("url", "condition", "value")))
    if rule.declaration != declaration:
        raise ValueError("browser sequence must use its canonical declaration")
    return rule


def browser_receipt_tools(checks: tuple[str, ...]) -> frozenset[str]:
    return frozenset(tool for check in checks if (rule := parse_browser_requirement(check)) is not None
                     for tool in rule.receipt_tools)
