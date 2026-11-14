"""Measure snapshot grouping RPC counts and elapsed time using real Chromium."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import ElementHandle, Route

from codey.automation import browser_session as session_module
from codey.automation.browser_observation import same_node
from codey.automation.browser_session import BrowserSession, _browser_call
from codey.research.browser_search import BrowserSearchProvider
from codey.types.json_value import JsonObject
from tools.completion_explanation_ab import production_source_digest, write_json


class ProbeBrowser(BrowserSearchProvider):
    def _guard_request(self, route: Route) -> None:
        route.fulfill(status=200, content_type='text/html', body='<article>Snapshot probe</article>')


class Options(argparse.Namespace):
    run_dir: Path
    repeats: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args(namespace=Options())
    if args.repeats < 1:
        parser.error('repeats must be positive')
    args.run_dir.mkdir(parents=True, exist_ok=False)
    source = production_source_digest()
    provider = ProbeBrowser(headless=True)
    browser = BrowserSession(provider)
    original = same_node
    comparisons = 0

    def counted(left: ElementHandle, right: ElementHandle) -> bool:
        nonlocal comparisons
        comparisons += 1
        return original(left, right)

    rows: list[JsonObject] = []
    try:
        assert browser.navigate('https://example.com/snapshot-group-probe').ok
        for count in (10, 40, 80):
            html = ''.join(f'<section aria-label="Order {i}"><button>Details</button></section>' for i in range(count))

            def install(html: str = html) -> None:
                browser._page_on_browser_thread().set_content(html)
            _browser_call(install)
            for repeat in range(args.repeats):
                comparisons = 0
                started = time.perf_counter()
                with patch.object(session_module, 'same_node', counted):
                    snapshot = browser.snapshot()
                elapsed = time.perf_counter() - started
                elements, contexts = snapshot.observation['elements'], snapshot.observation['contexts']
                assert snapshot.ok and isinstance(elements, list) and len(elements) == count
                assert isinstance(contexts, list) and len(contexts) == count
                row: JsonObject = {'controls': count, 'repeat': repeat + 1, 'same_node_calls': comparisons,
                                   'seconds': round(elapsed, 6)}
                rows.append(row)
                print(json.dumps(row), flush=True)
    finally:
        provider.close()
    summary: JsonObject = {'runs': list(rows), 'source_before': source, 'source_after': production_source_digest()}
    write_json(args.run_dir / 'summary.json', summary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
