"""Explicit live Zen gate through production connectors and headless workers."""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path

from codey.providers.zen import connection
from tools import local_model_gate_attempts as attempts
from tools.local_model_release_gate import _metadata

CASES = ("two_turn_json", "two_turn_sse", "chat", "read", "edit", "shell_approve", "reasoning")
DEFAULT_CASES = ("two_turn_json", "two_turn_sse", "read", "edit", "shell_approve", "reasoning")


def reasoning_choices(model: dict) -> list[dict]:
    """Match the production UI: efforts take precedence over a second toggle."""
    if model.get("efforts"):
        return [{}] + [{"effort": value} for value in model["efforts"]]
    if model.get("thinking_toggle"):
        return [{}] + [{"thinking": value} for value in (True, False)]
    return [{}]


def _conversation(selection, case: str, timeout: int) -> dict:
    provider = connection.open_selection(replace(selection, stream=case == "two_turn_sse"))
    try:
        first = provider.send("Remember the marker COPPER-742. Reply only ACK.", timeout=timeout)
        second = provider.send("What was the marker? Reply only the marker.", timeout=timeout)
        return {"ok": bool(first) and "COPPER-742" in second, "first_nonempty": bool(first),
                "marker_returned": "COPPER-742" in second,
                "history_items": len(provider.context_ledger.view)}
    finally:
        provider.close()


def _reasoning(selection, timeout: int) -> dict:
    provider = connection.open_selection(selection)
    try:
        reply = provider.send("Reply with the single word READY.", timeout=timeout)
        return {"ok": bool(reply.strip()), "nonempty": bool(reply.strip()),
                "marker_returned": "READY" in reply,
                "history_items": len(provider.context_ledger.view)}
    finally:
        provider.close()


def _run_case(model: dict, case: str, choice: dict, directory: Path, timeout: int) -> dict:
    started = time.monotonic()
    row = {"model": model["id"], "case": case, "choice": choice, "ok": False}
    try:
        selection = connection.capture_selection({"model": model["id"], **choice})
        if case.startswith("two_turn_"):
            row.update(_conversation(selection, case, timeout))
        elif case == "reasoning":
            row.update(_reasoning(selection, timeout))
        else:
            target = attempts.GateTarget(
                connection.endpoint_for_selection(selection), selection.model_id, selection.context_window_tokens,
                selection.context_reserve_tokens, selection.context_keep_recent_tokens,
                provider_id="zen", api_selection=selection.to_payload(), api_protocol=selection.protocol,
                request_timeout=timeout,
            )
            row.update(attempts.run_case_process(case, directory, target, timeout=timeout))
    except Exception as exc:
        row.update(error=str(exc), error_class=type(exc).__name__, status=getattr(exc, "status", None),
                   error_type=getattr(exc, "error_type", None), failure_stage="provider_or_harness_error")
    row["seconds"] = round(time.monotonic() - started, 3)
    return row


def _blocked(row: dict) -> bool:
    # A complete region refusal is terminal, not a reason to rotate exits or
    # spend another request on each remaining setting of the same model.
    return row.get("status") == 403 and row.get("error_type") == "RegionError"


def run_matrix(directory: Path, timeout: int, *, models: list[str] | None = None,
               cases: list[str] | None = None) -> bool:
    catalog = connection.refresh_models(force=True)
    if catalog.get("stale") or catalog.get("error") or not catalog.get("models"):
        raise RuntimeError("a fresh non-empty free-model catalog is required")
    available = {model["id"]: model for model in catalog["models"]}
    selected = list(available) if models is None else list(dict.fromkeys(models))
    if not selected or any(model not in available for model in selected):
        raise ValueError("every selected model must be in the current free catalog")
    requested = list(DEFAULT_CASES) if cases is None else list(dict.fromkeys(cases))
    if not requested or any(case not in CASES for case in requested):
        raise ValueError("select at least one supported live case")
    directory.mkdir(parents=True, exist_ok=False)
    attempts.write_json(directory / "catalog.json", catalog)
    results: list[dict] = []
    for model_id in selected:
        model = available[model_id]
        variant = directory / model_id
        variant.mkdir()
        selection = connection.capture_selection({"model": model_id})
        target = attempts.GateTarget(connection.endpoint_for_selection(selection), model_id,
            selection.context_window_tokens, selection.context_reserve_tokens, selection.context_keep_recent_tokens,
            provider_id="zen", api_selection=selection.to_payload(), api_protocol=selection.protocol)
        attempts.write_json(variant / "metadata.json", _metadata(target))
        blocked = False
        for case in requested:
            choices = reasoning_choices(model) if case == "reasoning" else [{}]
            for number, choice in enumerate(choices):
                if blocked:
                    row = {"model": model_id, "case": case, "choice": choice, "ok": False,
                           "not_run": True, "failure_stage": "blocked_by_prior_region_refusal"}
                else:
                    row = _run_case(model, case, choice, variant / f"{case}-{number}", timeout)
                    blocked = _blocked(row)
                results.append(row)
                attempts.write_json(directory / "summary.json", {
                    "ok": all(result.get("ok") is True for result in results), "models": selected,
                    "cases": requested, "attempts": len(results), "results": results,
                })
                # Redirected Windows consoles may still use a legacy codepage.
                print(json.dumps(row), flush=True)
    return all(row.get("ok") is True for row in results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="new artifact directory")
    parser.add_argument("--timeout", type=int, default=180, help="seconds per send or headless case")
    parser.add_argument("--models", nargs="+", help="current free model IDs; default: all")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(DEFAULT_CASES))
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    return 0 if run_matrix(args.run_dir.resolve(), args.timeout, models=args.models, cases=args.cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
