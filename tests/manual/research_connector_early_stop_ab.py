"""Offline 2A A/B for connector early-stop behaviour.

The candidate removes only the connector-hit early return.  It always invokes
the existing browser provider under the same result limit, then uses the
production merge/deduplication function.  No production default is changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from codey.research import connector_search as production
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
    name = "fixture-browser"

    def __init__(self, rows: tuple[JsonObject, ...]) -> None:
        self.rows = rows
        self.calls = 0

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        del query
        self.calls += 1
        return list(self.rows[:limit])


class _ConnectorFixture(production.ConnectorAwareSearchProvider):
    def __init__(self, browser: _FakeProvider, connector_rows: tuple[JsonObject, ...]) -> None:
        super().__init__(browser, connector_ids=(), connector_limit=2)
        self.connector_rows = connector_rows

    def _connector_search_results(self, query: str, *, limit: int, safe_query=None):
        del query, safe_query
        return list(self.connector_rows[:limit])

    def search(self, query: str, limit: int = 8) -> list[JsonObject]:
        """Freeze the historical early-stop control after production moved on."""
        result_limit = production._bounded_limit(limit, default=8, upper=production.MAX_CONNECTOR_HITS)
        rows = self._connector_search_results(query, limit=min(self.connector_limit, result_limit))
        if len(rows) >= production.CONNECTOR_MIN_RESULTS:
            return rows
        return production._merge_results(rows, self._base_search(query, limit=result_limit), limit=result_limit)


def _candidate_search(self: _ConnectorFixture, query: str, limit: int = 8) -> list[JsonObject]:
    query = " ".join((query or "").split())
    if not query:
        return []
    result_limit = production._bounded_limit(limit, default=8, upper=production.MAX_CONNECTOR_HITS)
    connector_rows = self._connector_search_results(query, limit=min(self.connector_limit, result_limit))
    browser_rows = self._base_search(query, limit=result_limit)
    return production._merge_results(connector_rows, browser_rows, limit=result_limit)


def run_case(case: SearchCase, arm: str) -> JsonObject:
    browser = _FakeProvider(case.browser_hits)
    provider = _ConnectorFixture(browser, case.connector_hits)
    if arm == "browser_after_connector":
        provider.search = _candidate_search.__get__(provider, _ConnectorFixture)
    rows = provider.search("versioned limitation", limit=case.limit)
    return {
        "case": case.name,
        "arm": arm,
        "connector_hits": len(case.connector_hits),
        "browser_calls": browser.calls,
        "result_urls": [row.get("url", "") for row in rows],
        "browser_material_added": any("official.example.org" in str(row.get("url")) for row in rows),
        "result_count": len(rows),
        "limit_preserved": len(rows) <= case.limit,
    }


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
        "rows": rows,
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
