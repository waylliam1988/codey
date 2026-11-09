"""Record actual headed search results for configured engines; relevance needs review."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.research.browser_search import BrowserSearchProvider, SearchUnavailableError
from codey.research.web_sources import BrowserSource
from codey.types.json_value import JsonObject

QUERIES = ("福州美食 推荐 吃什么 福州小吃", "东百中心 福州 附近 景点 好玩 餐厅 商场",
           "成都太古里 附近 游玩 推荐", "杭州美食 本地小吃")


class Arguments(argparse.Namespace):
    run_dir: Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(namespace=Arguments())
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    reports: list[JsonObject] = []
    for number, query in enumerate(QUERIES):
        engines = ("bing", "duckduckgo") if number % 2 == 0 else ("duckduckgo", "bing")
        for engine in engines:
            provider: BrowserSource = BrowserSearchProvider(engine=engine, headless=False, isolated=False)
            started = time.monotonic()
            report: JsonObject = {"engine": engine, "query": query, "relevance_review_required": True}
            try:
                results = provider.search(query, limit=8)
                report.update({"results": [row for row in results], "acquisition_ok": bool(results)})
            except (SearchUnavailableError, TimeoutError) as exc:
                report.update({"results": [], "acquisition_ok": False, "error": str(exc)})
            finally:
                provider.close()
            report["seconds"] = round(time.monotonic() - started, 3)
            reports.append(report)
            (directory / "summary.json").write_text(json.dumps({"queries": [row for row in reports]},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({key: report.get(key) for key in ("engine", "query", "acquisition_ok", "seconds", "error")}),
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
