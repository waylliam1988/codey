"""Measure bounded observations through the production task browser process."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from codey.automation.browser_contracts import RouteDecision
from codey.types.json_value import JsonObject
from tools.browser_experiment_sources import ResumableBrowserSearchProvider
from tools.completion_explanation_ab import production_source_digest, write_json


class ProbeBrowser(ResumableBrowserSearchProvider):
    def _guard_request(self, route: RouteDecision) -> None:
        route.fulfill(content_type='text/html', body='<article>Snapshot probe</article>')


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
    browser = provider.browser
    rows: list[JsonObject] = []
    try:
        assert browser.navigate('https://example.com/snapshot-group-probe').ok
        for count in (10, 40, 80):
            html = ''.join(f'<section aria-label="Order {i}"><button>Details</button></section>' for i in range(count))
            assert browser.evaluate('(() => {document.open();document.write(' + json.dumps(html) + ');document.close();})()').ok
            for repeat in range(args.repeats):
                started = time.perf_counter()
                snapshot = browser.snapshot()
                elapsed = time.perf_counter() - started
                elements, contexts = snapshot.observation['elements'], snapshot.observation['contexts']
                assert snapshot.ok and isinstance(elements, list) and len(elements) == count
                assert isinstance(contexts, list) and len(contexts) == count
                row: JsonObject = {'controls': count, 'contexts': len(contexts), 'repeat': repeat + 1,
                    'seconds': round(elapsed, 6), 'scope': 'production_async_worker_observation'}
                rows.append(row)
                print(json.dumps(row), flush=True)
    finally:
        provider.close()
    write_json(args.run_dir / 'summary.json', {'runs': rows, 'source_before': source,
        'source_after': production_source_digest()})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
