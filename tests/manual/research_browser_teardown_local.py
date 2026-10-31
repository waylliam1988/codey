"""Exercise real isolated-browser route teardown, without a model or web server.

Run from the repository root with --directory <new artifact directory>.
The child keeps asyncio debug output intact; the parent fails on orphaned work.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
from pathlib import Path

from codey.research.browser_search import BrowserSearchProvider, _search_browser_call


def _burst(provider: BrowserSearchProvider) -> None:
    page = provider._ensure_fetch_page_on_browser_thread("https://www.sqlite.org/wal.html")
    # The production guard blocks these destinations. The burst makes no model
    # requests and sends no requests to public sites or local application servers.
    page.set_content("".join(f'<img src="http://127.0.0.1:9/probe-{n}">' for n in range(100)),
                     wait_until="domcontentloaded")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=12)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 100:
        parser.error("cycles must be 1..100")
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.worker:
        for cycle in range(args.cycles):
            provider = BrowserSearchProvider(profile_dir=args.directory / "profile")
            try:
                _search_browser_call(_burst, provider)
            finally:
                provider.close()
                _search_browser_call(gc.collect)
                print(f"cycle={cycle} closed", flush=True)
        return
    env = {**os.environ, "PYTHONASYNCIODEBUG": "1"}
    log = args.directory / "console.log"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.run([sys.executable, "-X", "utf8", "-u", "-m",
            "tests.manual.research_browser_teardown_local", "--worker", "--directory", str(args.directory),
            "--cycles", str(args.cycles)], env=env, stdout=output, stderr=subprocess.STDOUT,
            timeout=max(120, args.cycles * 15), check=False)
    text = log.read_text(encoding="utf-8")
    failures = {key: text.count(key) for key in (
        "TargetClosedError", "Future exception was never retrieved", "Task was destroyed but it is pending")}
    result = {"cycles": args.cycles, "closed": text.count(" closed\n"),
              "exit_code": process.returncode, "warnings": failures}
    (args.directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    if process.returncode or result["closed"] != args.cycles or any(failures.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
