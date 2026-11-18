"""Exercise existing browser scenarios without any explicit capability opt-in."""
from __future__ import annotations

import argparse
from pathlib import Path

from tools.browser_production_live_gate import run_case, select_target
from tools.completion_explanation_ab import production_source_digest, service_state, write_json


class Options(argparse.Namespace):
    run_dir: Path
    mode: str
    timeout: float
    base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("chat", "project", "hybrid", "research", "all"), default="all")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Options())
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = select_target(args.timeout, args.base_url)
    modes = ("chat", "project", "hybrid", "research") if args.mode == "all" else (args.mode,)
    before = production_source_digest()
    write_json(root / "protocol.json", {
        "modes": modes, "model": target.model, "seed": target.seed, "timeout": target.timeout,
        "requested_capabilities": [], "source_before": before,
        "scope": "real local model and Chromium, existing controlled page and production entry/kernel",
        "limits": "Explicit action task; no claim of natural website reliability or visible-window validation.",
    })
    results: list[dict[str, object]] = []
    for mode in modes:
        backend = service_state(target.base_url)
        if backend.get("idle") != 1 or backend.get("queue") != 0:
            write_json(root / "scheduler-halt.json", {"mode": mode, "backend": backend})
            break
        write_json(root / f"{mode}-backend-before.json", backend)
        print(f"Starting {mode}", flush=True)
        result = run_case(root / mode, mode, target, requested_capabilities=(),
                          strict_research=mode == "research", declare_required_tools=False)
        results.append(result)
        print(f"{mode}: passed={result.get('passed')} stop={result.get('stop_reason')}", flush=True)
        summary: dict[str, object] = {"results": results, "source_before": before,
                                      "source_after": production_source_digest(),
                                      "backend_after": service_state(target.base_url)}
        write_json(root / "summary.json", summary)
    return 0 if len(results) == len(modes) and all(row.get("passed") is True for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
