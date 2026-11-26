"""Frozen early-stop control compared with the current single coordinator."""
from __future__ import annotations

from dataclasses import dataclass

from codey.research import web_sources as production
from codey.research.connector_search import ScholarlySources
from codey.research.search_contracts import SearchOutcome, complete_search
from codey.research.search_profiles import SearchProfile
from codey.research.search_provenance import SearchAttempt
from codey.types.json_value import JsonObject

ARMS = ("baseline", "browser_after_connector")

@dataclass(frozen=True)
class SearchCase:
    name: str
    connector_hits: tuple[JsonObject, ...]
    browser_hits: tuple[JsonObject, ...]
    limit: int = 4
    early_stop_expected: bool = True


def cases() -> tuple[SearchCase, ...]:
    return tuple(
        SearchCase(
            name=f"formal_{index:02d}",
            connector_hits=(
                {"url": f"https://pubmed.ncbi.nlm.nih.gov/{index}1", "title": f"Connector {index}A"},
                {"url": f"https://pubmed.ncbi.nlm.nih.gov/{index}2", "title": f"Connector {index}B"},
            ),
            browser_hits=(
                {"url": f"https://official.example.org/rule/{index}", "title": f"Official rule {index}"},
                {"url": f"https://docs.example.org/version/{index}", "title": f"Version note {index}"},
            ),
        )
        for index in range(1, 13)
    )


class _FakeProvider:
    def __init__(self, rows: tuple[JsonObject, ...]) -> None:
        self.rows = rows
        self.calls = 0
    def search(self, query: str, *, limit: int, profile: SearchProfile, deadline: float) -> SearchOutcome:
        self.calls += 1
        return complete_search(query, list(self.rows), SearchAttempt(query, profile.name), limit, explicit_empty=not self.rows)
    def fetch(self, url: str) -> dict[str, object]:
        raise AssertionError("search fixture cannot open documents")
    def close(self) -> None:
        pass


class _ScholarlyFixture(ScholarlySources):
    def __init__(self, rows: tuple[JsonObject, ...]) -> None:
        super().__init__(connector_ids=())
        self.rows = rows
    def search(self, query: str, *, limit: int, deadline: float) -> SearchOutcome:
        return complete_search(query, list(self.rows), SearchAttempt(query, "fixture", coverage="scholarly"),
                               limit, explicit_empty=not self.rows)


class _ConnectorFixture(production.WebSourceProvider):
    def __init__(self, browser: _FakeProvider, connector_rows: tuple[JsonObject, ...]) -> None:
        super().__init__(browser_factory=lambda: browser, scholarly=_ScholarlyFixture(connector_rows))


def run_case(case: SearchCase, arm: str) -> JsonObject:
    browser = _FakeProvider(case.browser_hits)
    if arm == "baseline" and len(case.connector_hits) >= 2:
        rows = list(case.connector_hits[:case.limit])  # Frozen historical control, never production dispatch.
    else:
        rows = _ConnectorFixture(browser, case.connector_hits).search("versioned limitation", limit=case.limit).rows
    return {"case": case.name, "arm": arm, "connector_hits": len(case.connector_hits), "browser_calls": browser.calls,
        "result_urls": [row.get("url", "") for row in rows],
        "browser_material_added": any("official.example.org" in str(row.get("url")) for row in rows),
        "result_count": len(rows), "limit_preserved": len(rows) <= case.limit}


def summarize() -> JsonObject:
    rows = [run_case(case, arm) for case in cases() for arm in ARMS]
    by_arm = {
        arm: [row for row in rows if row["arm"] == arm]
        for arm in ARMS
    }
    return {
        "schema_version": 1,
        "arms": list(ARMS),
        "cases": len(cases()),
        "rows": [row for row in rows],
        "mechanism": {
            "baseline_early_stop": all(row["browser_calls"] == 0 for row in by_arm[ARMS[0]]),
            "candidate_browser_called": all(row["browser_calls"] == 1 for row in by_arm[ARMS[1]]),
            "candidate_adds_material": all(row["browser_material_added"] for row in by_arm[ARMS[1]]),
            "limit_preserved": all(row["limit_preserved"] for row in rows),
        },
    }


if __name__ == "__main__":
    import json

    print(json.dumps(summarize(), ensure_ascii=False, indent=2))
