"""Validated, immutable engine protocols; loaded once per provider."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from codey.types.json_value import JsonObject, parse_json_object, require_json_string, require_json_strings


@dataclass(frozen=True)
class SearchProfile:
    name: str
    home_url: str
    allowed_hosts: tuple[str, ...]
    query_input: str
    submit: str
    result_root: str
    result_selector: str
    link_selector: str
    snippet_selector: str
    empty_selector: str
    challenge_selector: str
    empty_markers: tuple[str, ...]

    def payload(self) -> JsonObject:
        return {"query_input": self.query_input, "submit": self.submit, "result_root": self.result_root,
                "result_selector": self.result_selector, "link_selector": self.link_selector,
                "snippet_selector": self.snippet_selector, "empty_selector": self.empty_selector,
                "challenge_selector": self.challenge_selector, "empty_markers": list(self.empty_markers)}

    def allows(self, url: str) -> bool:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname in self.allowed_hosts and not parsed.username


def _field(row: JsonObject, key: str) -> str:
    value = require_json_string(row[key], key)
    if not value:
        raise ValueError("empty search profile field: " + key)
    return value


def load_profiles() -> tuple[SearchProfile, ...]:
    row = parse_json_object(Path(__file__).with_name("search_profiles.json").read_text(encoding="utf-8"))
    default = require_json_string(row["default_engine"], "default engine")
    engines = row["engines"]
    if not isinstance(engines, dict) or default not in engines:
        raise ValueError("search configuration needs a declared default engine")
    profiles: dict[str, SearchProfile] = {}
    for name, raw in engines.items():
        if not isinstance(raw, dict):
            raise ValueError("invalid search engine configuration")
        profile = SearchProfile(name, _field(raw, "home_url"), tuple(require_json_strings(raw["allowed_hosts"], "hosts")),
            _field(raw, "query_input"), _field(raw, "submit"), _field(raw, "result_root"), _field(raw, "result_selector"),
            _field(raw, "link_selector"), _field(raw, "snippet_selector"), _field(raw, "empty_selector"), _field(raw, "challenge_selector"),
            tuple(require_json_strings(raw["empty_markers"], "empty markers")))
        if not profile.allows(profile.home_url):
            raise ValueError("search homepage is outside its allowed hosts")
        profiles[name] = profile
    return (profiles[default], *(profile for name, profile in profiles.items() if name != default))
