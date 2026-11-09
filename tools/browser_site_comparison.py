"""Real-site HTTP-first/browser-first acquisition comparison without model loops."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from codey.research.chat_web import ChatWebProvider
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, canonical_json_object
from tools.browser_experiment_interaction import BrowserInteraction
from tools.browser_experiment_sources import ResumableBrowserSearchProvider

SITES = (
    ("zhihu_new", "https://zhuanlan.zhihu.com/p/22937491675"),
    ("toutiao", "https://www.toutiao.com/article/7419205545988456994/"),
    ("zhihu_old", "https://zhuanlan.zhihu.com/p/399217412"),
    ("sohu", "https://www.sohu.com/a/915586727_121911505"),
    ("python_docs", "https://docs.python.org/3/library/asyncio-task.html"),
    ("playwright_docs", "https://playwright.dev/python/docs/intro"),
    ("pdf", "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"),
)


def run(directory: Path, repeats: int) -> list[JsonObject]:
    rows: list[JsonObject] = []
    for repeat in range(1, repeats + 1):
        for name, url in SITES:
            for candidate in ([False, True] if repeat % 2 else [True, False]):
                arm = "browser_first" if candidate else "http_first"
                browser = ResumableBrowserSearchProvider(headless=True, http_fallback=False)

                def browser_factory(selected: ResumableBrowserSearchProvider = browser) -> ResumableBrowserSearchProvider:
                    return selected

                provider = browser if candidate else ChatWebProvider(browser_factory=browser_factory)
                started = time.monotonic()
                observation: JsonObject = {}
                row: JsonObject
                try:
                    with cancellation.deadline_scope(time.monotonic() + 90):
                        outcome = ResearchSourceGateway(provider, ResearchLedger()).open(url)
                        text = outcome.document.text if outcome.document is not None else ""
                        if candidate:
                            snapshot = BrowserInteraction(browser).snapshot() if browser._session is not None else None
                            observation = snapshot.observation if snapshot is not None else {}
                        row = canonical_json_object({"site": name, "url": url, "arm": arm, "repeat": repeat,
                            "status": outcome.status, "failure_code": outcome.failure_code if outcome.status != "ok" else "",
                            "detail": outcome.detail, "text_chars": len(text),
                            "title": outcome.document.title if outcome.document else "",
                            "kind": outcome.document.content_kind if outcome.document else "",
                            "acquisition": outcome.acquisition, "seconds": round(time.monotonic() - started, 3)})
                        (directory / f"{name}-{repeat}-{arm}.txt").write_text(text, encoding="utf-8")
                except Exception as exc:
                    error = str(exc)
                    row = {"site": name, "url": url, "arm": arm, "repeat": repeat,
                           "status": "exception", "detail": error}
                finally:
                    provider.close()
                (directory / f"{name}-{repeat}-{arm}.json").write_text(
                    json.dumps({"result": row, "page_observation": observation}, ensure_ascii=False, indent=2), encoding="utf-8")
                rows.append(row)
                (directory / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(row, ensure_ascii=False), flush=True)
    (directory / "summary.json").write_text(json.dumps({"rows": rows,
        "scope": "real websites, fresh sessions, no human verification or login, acquisition only",
        "limits": "Cannot certify same-session accessibility, model quality, or global browser-first defaults."},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    run(directory, args.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
