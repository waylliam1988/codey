"""Explicit browser prerequisites carried by a frozen task policy."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from codey.policies.network import check_fetch_url
from codey.types.json_value import decode_json_object, require_json_integer, require_json_string

RESEARCH_PREFIX = "browser_research_sequence:"
ROUNDS_PREFIX = "browser_page_rounds:"
PAGE_TRANSITIONS = frozenset({"browser_navigate", "browser_tab_new", "browser_tab_switch"})


@dataclass(frozen=True)
class _BrowserPage:
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
    def wait_step(self) -> str:
        return f"Use browser_wait with condition={self.condition!r} and value={self.value!r}."


@dataclass(frozen=True)
class BrowserResearchSequence(_BrowserPage):
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


@dataclass(frozen=True)
class BrowserPageRounds(_BrowserPage):
    rounds: int
    fill_name: str
    fill_value: str
    click_name: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.rounds) is not int or not 1 <= self.rounds <= 8:
            raise ValueError("browser page rounds must be an integer between 1 and 8")
        if any(not field.strip() or len(field) > 512 for field in (self.fill_name, self.fill_value, self.click_name)):
            raise ValueError("browser rounds require bounded, nonempty fill and click declarations")

    @property
    def declaration(self) -> str:
        return ROUNDS_PREFIX + json.dumps({"url": self.url, "condition": self.condition, "value": self.value,
            "rounds": self.rounds, "fill_name": self.fill_name, "fill_value": self.fill_value, "click_name": self.click_name},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def check_id(self) -> str:
        return ROUNDS_PREFIX + hashlib.sha256(self.declaration.encode()).hexdigest()[:24]

    @property
    def receipt_tools(self) -> frozenset[str]:
        return PAGE_TRANSITIONS | {"browser_snapshot", "browser_fill", "browser_click", "browser_wait", "browser_read"}

    @property
    def guidance(self) -> str:
        return (f"Complete {self.rounds} independent browser rounds. Each starts with browser_navigate to {self.url}. "
                f"Use browser_snapshot to find {self.fill_name!r}, then fill it with {self.fill_value!r}. "
                f"Take a fresh snapshot and click {self.click_name!r}. {self.wait_step} "
                "Finish each round with a complete browser_read on that same document.")


def require_browser_research_sequence(*, url: str, condition: str, value: str) -> str:
    return BrowserResearchSequence(url, condition, value).declaration


def require_browser_page_rounds(*, url: str, condition: str, value: str, rounds: int,
                               fill_name: str, fill_value: str, click_name: str) -> str:
    return BrowserPageRounds(url, condition, value, rounds, fill_name, fill_value, click_name).declaration


def is_browser_requirement(declaration: str) -> bool:
    return declaration.startswith((RESEARCH_PREFIX, ROUNDS_PREFIX))


def parse_browser_requirement(declaration: str) -> BrowserResearchSequence | BrowserPageRounds | None:
    if not is_browser_requirement(declaration):
        return None
    name, _, payload = declaration.partition(":")
    row = decode_json_object(json.loads(payload))
    keys = ("url", "condition", "value")
    round_keys = ("fill_name", "fill_value", "click_name")
    rule: BrowserResearchSequence | BrowserPageRounds
    if name + ":" == ROUNDS_PREFIX:
        if set(row) != {*keys, *round_keys, "rounds"}:
            raise ValueError("invalid browser page rounds fields")
        url, condition, value = (require_json_string(row[key], key) for key in keys)
        fill_name, fill_value, click_name = (require_json_string(row[key], key) for key in round_keys)
        rule = BrowserPageRounds(url, condition, value, require_json_integer(row["rounds"], "rounds"),
                                 fill_name, fill_value, click_name)
    else:
        if set(row) != set(keys):
            raise ValueError("invalid browser research sequence fields")
        rule = BrowserResearchSequence(*(require_json_string(row[key], key) for key in keys))
    if rule.declaration != declaration:
        raise ValueError("browser sequence must use its canonical declaration")
    return rule


def browser_receipt_tools(checks: tuple[str, ...]) -> frozenset[str]:
    return frozenset(tool for check in checks if (rule := parse_browser_requirement(check)) is not None
                     for tool in rule.receipt_tools)
