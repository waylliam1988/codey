"""Live PubMed follow-up using the existing model action probe unchanged."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from codey.research.web_sources import WebSourceProvider
from tools import model_web_action_probe as probe

NAME = "pubmed_rapamycin"
TASK = "帮我去PubMed上看看雷帕霉素的论文"


def production_hashes() -> dict[str, str]:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path("codey").rglob("*.py"))}


def logged_page(page: dict[str, object]) -> dict[str, object]:
    return {key: ({"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                  if isinstance(value, bytes) else value) for key, value in page.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--protocol", choices=("native", "text"), default="native")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.prepare:
        root.mkdir(parents=True, exist_ok=False)
        with urlopen(probe.ENDPOINT + "/models", timeout=5) as response:
            models = json.load(response)
        if models["data"][0]["id"] != probe.MODEL:
            raise ValueError("Loaded model changed")
        probe.write(root / "protocol.json", {"task_verbatim": TASK, "model": probe.MODEL,
            "matrix": ["native", "text"], "source_mode": "real search and fetch, no fixture responses",
            "temperature": 0, "stream": False, "output_tokens": 8192, "context": 262144,
            "reserve": 32768, "keep": 32000, "max_turns": 8, "request_timeout": 120,
            "forced_source_requirement": False, "new_task_instructions": False,
            "scope": "Normal production Chat; read PubMed records/abstracts is distinct from full paper text",
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "base_runner_sha256": hashlib.sha256(Path(probe.__file__).read_bytes()).hexdigest(),
            "models": models, "backend_before": probe.backend_status(),
            "production_hashes": production_hashes(),
            "limits": "One natural task per protocol, independent empty states; no repeated samples or source requirement gates."})
        print(str(root), flush=True)
        return

    probe.CASES[NAME] = probe.Case(TASK, "PubMed", "", (), "", ())
    cell = root / f"live-{NAME}-{args.protocol}"
    search_original, fetch_original = WebSourceProvider.search, WebSourceProvider.fetch

    def search(self: WebSourceProvider, query: str, *, limit: int = 8) -> list[dict[str, object]]:
        hits = search_original(self, query, limit=limit)
        probe.append(cell / "live-acquisitions.jsonl", {"kind": "search", "query": query, "hits": hits})
        return hits

    def fetch(self: WebSourceProvider, url: str) -> dict[str, object]:
        page = fetch_original(self, url)
        probe.append(cell / "live-acquisitions.jsonl", {"kind": "fetch", "url": url, "page": logged_page(page)})
        return page

    with ExitStack() as stack:
        stack.enter_context(patch.object(WebSourceProvider, "search", search))
        stack.enter_context(patch.object(WebSourceProvider, "fetch", fetch))
        probe.run_cell(root, NAME, args.protocol, True)
    probe.write(cell / "production-hashes-after.json", production_hashes())


if __name__ == "__main__":
    main()
