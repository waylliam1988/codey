"""Verify every current free Zen model through production entry and approval."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools import local_model_gate_attempts as attempts
from tools.local_model_release_gate import _metadata


def run_matrix(directory: Path, timeout: int) -> bool:
    from codey.providers.zen.connection import capture_selection, model_payload
    from codey.providers.zen.identity import ZEN_BASE_URL

    directory.mkdir(parents=True, exist_ok=False)
    catalog = model_payload()
    if catalog.get("stale") or catalog.get("error") or not catalog.get("models"):
        raise RuntimeError("a fresh non-empty free-model catalog is required")
    attempts.write_json(directory / "catalog.json", catalog)
    results = []
    for model in catalog["models"]:
        settings = [None, False, True] if model.get("thinking_toggle") else [None]
        for thinking in settings:
            chosen = {"model": model["id"]}
            if thinking is not None:
                chosen["thinking"] = thinking
            selection = capture_selection(chosen)
            target = attempts.GateTarget(
                ZEN_BASE_URL, selection.model_id, selection.context_window_tokens,
                selection.context_reserve_tokens, selection.context_keep_recent_tokens,
                provider_id="zen", api_selection=selection.to_payload(), api_protocol=selection.protocol,
                request_timeout=timeout,
            )
            label = model["id"] + ("-default" if thinking is None else "-thinking-" + str(thinking).lower())
            variant = directory / label
            variant.mkdir()
            attempts.write_json(variant / "metadata.json", _metadata(target))
            for case in ("chat", "read", "shell_approve"):
                result = attempts.run_case_process(case, variant / case, target, timeout=timeout)
                result.update(model=model["id"], thinking=thinking)
                results.append(result)
                ok = all(row.get("ok") is True for row in results)
                attempts.write_json(directory / "summary.json", {"ok": ok, "models": len(catalog["models"]),
                    "attempts": len(results), "results": results})
                print(json.dumps({"model": model["id"], "thinking": thinking, "case": case,
                    "ok": result.get("ok"), "failure_stage": result.get("failure_stage", "")}), flush=True)
    return all(row.get("ok") is True for row in results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="new artifact directory")
    parser.add_argument("--timeout", type=int, default=120, help="total seconds per case")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    return 0 if run_matrix(args.run_dir.resolve(), args.timeout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
