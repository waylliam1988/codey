"""Offline 2B A/B for bounded abstract-to-full-text recovery.

The candidate is an experiment-only adapter.  It tries one explicit public
full-text URL attached to the selected source, preserves the abstract on any
failure, and records the returned URL/content kind.  It does not change the
production source gateway or add a dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ARMS = ("abstract_baseline", "public_fulltext_recovery")


@dataclass(frozen=True)
class FullTextCase:
    name: str
    abstract: str
    fulltext_url: str
    fulltext: str | None
    needs_fulltext: bool


def cases() -> tuple[FullTextCase, ...]:
    rows = []
    for index in range(1, 9):
        rows.append(FullTextCase(
            f"needs_fulltext_{index:02d}", f"Abstract {index}: reports an effect but omits method limit.",
            f"https://public.example.org/article/{index}", f"Full text {index}: method limit is LIMIT-{index}.", True,
        ))
    rows.extend([
        FullTextCase("abstract_sufficient_09", "Abstract 9: explicitly reports the requested limit LIMIT-9.",
                     "https://public.example.org/article/9", "Full text 9", False),
        FullTextCase("abstract_sufficient_10", "Abstract 10: directly states the requested condition CONDITION-10.",
                     "https://public.example.org/article/10", "Full text 10", False),
        FullTextCase("unavailable_11", "Abstract 11: omits the requested method.",
                     "https://public.example.org/article/11", None, True),
        FullTextCase("unavailable_12", "Abstract 12: omits the requested method.",
                     "https://public.example.org/article/12", None, True),
    ])
    return tuple(rows)


def _recover(case: FullTextCase, fetch: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    try:
        page = fetch(case.fulltext_url)
    except Exception as exc:  # bounded recovery falls back to the known abstract
        return {"text": case.abstract, "content_kind": "abstract", "url": "", "error": type(exc).__name__}
    if page.get("status") != "ok" or not isinstance(page.get("text"), str) or not page["text"].strip():
        return {"text": case.abstract, "content_kind": "abstract", "url": "", "error": "unavailable"}
    return {"text": page["text"], "content_kind": str(page.get("content_kind") or "html"),
            "url": str(page.get("url") or case.fulltext_url), "error": ""}


def run_case(case: FullTextCase, arm: str) -> dict[str, Any]:
    def fetch(_url: str) -> dict[str, Any]:
        if case.fulltext is None:
            return {"status": "error", "detail": "public full text unavailable"}
        return {"status": "ok", "url": case.fulltext_url, "content_kind": "html", "text": case.fulltext}

    result = {"text": case.abstract, "content_kind": "abstract", "url": "", "error": ""}
    if arm == "public_fulltext_recovery":
        result = _recover(case, fetch)
    return {"case": case.name, "arm": arm, "needs_fulltext": case.needs_fulltext,
            "content_kind": result["content_kind"], "recovered": result["content_kind"] != "abstract",
            "url_preserved": result["content_kind"] == "abstract" or result["url"] == case.fulltext_url,
            "answer_material_present": (not case.needs_fulltext) or ("Full text" in result["text"]),
            "fallback_preserved": case.fulltext is None and result["content_kind"] == "abstract"}


def summarize() -> dict[str, Any]:
    rows = [run_case(case, arm) for case in cases() for arm in ARMS]
    candidate = [row for row in rows if row["arm"] == ARMS[1]]
    return {"schema_version": 1, "arms": list(ARMS), "cases": len(cases()), "rows": rows,
            "mechanism": {"eligible_cases": sum(case.needs_fulltext for case in cases()),
                          "recovered": sum(row["recovered"] for row in candidate if row["needs_fulltext"]),
                          "identity_preserved": all(row["url_preserved"] for row in candidate),
                          "fallback_preserved": all(row["fallback_preserved"] for row in candidate if row["case"].startswith("unavailable"))}}


if __name__ == "__main__":
    import json

    print(json.dumps(summarize(), ensure_ascii=False, indent=2))
