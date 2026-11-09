"""Read real sources through an explicitly selected existing user CDP browser."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from codey.automation.browser import CdpEndpoint
from codey.research.browser_search import _search_browser_call
from codey.research.ledger import ResearchLedger
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_value
from tools.browser_experiment_binding import bind_page
from tools.browser_experiment_interaction import BrowserInteraction
from tools.browser_experiment_sources import BrowserCaptureGateway, ResumableBrowserSearchProvider

URLS = ("https://zhuanlan.zhihu.com/p/399217412", "https://zhuanlan.zhihu.com/p/22937491675")


def targets(port: int) -> dict[str, str]:
    with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
        payload = decode_json_value(json.loads(response.read()))
    if not isinstance(payload, list):
        raise ValueError("CDP target response must be a list")
    return {str(row["id"]): str(row.get("url") or "") for row in payload
            if isinstance(row, dict) and row.get("type") == "page"}


def run(directory: Path, port: int) -> JsonObject:
    initial = targets(port)
    provider = ResumableBrowserSearchProvider(cdp_port=port, isolated=False,
                                              bring_to_front=False)

    def endpoint(**kwargs: object) -> CdpEndpoint:
        return CdpEndpoint(port, None)

    def binding() -> JsonObject:
        session = provider._session
        page = provider._fetch_page
        if session is None or page is None:
            raise ValueError("fetch did not retain a bound page")
        target = bind_page(session, page)
        return {"browser_token": target.browser_token, "context_token": target.context_token,
                "page_token": target.page_token, "owns_browser": session.owns_browser,
                "owns_context": session.owns_context, "owns_session_page": session.owns_page,
                "fetch_is_session_page": page is session.page}

    rows: list[JsonObject] = []
    try:
        with patch("codey.automation.browser._ensure_cdp_endpoint", endpoint):
            for number, url in enumerate(URLS, 1):
                navigation = provider.fetch(url)
                (directory / f"navigation-{number}.json").write_text(
                    json.dumps(navigation, ensure_ascii=False, indent=2), encoding="utf-8")
                identity = _search_browser_call(binding)
                interaction = BrowserInteraction(provider)
                observed = interaction.snapshot()
                capture = interaction.capture()
                ledger = ResearchLedger()
                outcome = BrowserCaptureGateway(provider, ledger).accept_capture(url, capture)
                text = outcome.document.text if outcome.document is not None else ""
                (directory / f"source-{number}.txt").write_text(text, encoding="utf-8")
                row = canonical_json_object({"url": url, "navigation": navigation, "binding": identity,
                    "observation": observed.observation, "capture_status": outcome.status,
                    "failure_code": outcome.failure_code if outcome.status != "ok" else "", "text_chars": len(text),
                    "binding_unchanged": _search_browser_call(binding) == identity})
                rows.append(row)
                (directory / f"source-{number}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({key: row[key] for key in ("url", "capture_status", "failure_code", "text_chars", "binding_unchanged")},
                                 ensure_ascii=False), flush=True)
    finally:
        provider.close()
    remaining = targets(port)
    report = canonical_json_object({"cdp_port": port, "initial_targets": initial, "remaining_targets": remaining,
        "borrowed_pages_preserved": initial.keys() <= remaining.keys(), "rows": rows,
        "scope": "existing user browser, task-created source pages, no login credentials captured",
        "limits": "This verifies the selected CDP browser/context, not another same-brand window."})
    (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report = run(directory, args.port)
    return 0 if report["borrowed_pages_preserved"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
