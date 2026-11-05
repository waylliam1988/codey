"""Explicit live Cline gate through production factories and headless cases."""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path

from codey.providers.cline.connection import capture_selection, open_selection
from codey.providers.cline.identity import INFERENCE_URL
from codey.providers.cline.runtime import runtime
from tools import local_model_gate_attempts as attempts


def login() -> None:
    resource = runtime()
    snapshot = resource.auth.snapshot()
    if snapshot["state"] == "ready":
        return
    resource.auth.start_login()
    print("Cline sign-in opened in the system browser; waiting for authorization.", flush=True)
    deadline = time.monotonic() + 330
    while time.monotonic() < deadline:
        snapshot = resource.auth.snapshot()
        if snapshot["state"] == "ready":
            print("Cline credentials saved in the operating system credential store.", flush=True)
            return
        if snapshot["state"] not in {"starting", "awaiting_authorization", "exchanging"}:
            raise RuntimeError("Cline sign-in ended: " + str(snapshot.get("error_code") or snapshot["state"]))
        time.sleep(1)
    resource.auth.cancel_login(snapshot["attempt_id"])
    raise TimeoutError("Cline sign-in deadline exceeded")


def run(directory: Path, model: str, cases: list[str], timeout: int) -> bool:
    directory.mkdir(parents=True, exist_ok=False)
    resource = runtime()
    resource.catalog.refresh(force=True)
    selection = capture_selection({"model": model})
    results = []
    # Both ordinary two-turn modes use the real connector and HTTP transport.
    for stream in (False, True):
        provider = open_selection(replace(selection, stream=stream))
        try:
            first = provider.send("Remember the marker COPPER-742. Reply only ACK.", timeout=timeout)
            second = provider.send("What was the marker? Reply only the marker.", timeout=timeout)
            ok = "COPPER-742" in second
            results.append({"case": "two_turn_sse" if stream else "two_turn_json", "ok": ok,
                            "first_nonempty": bool(first), "history_items": len(provider.context_ledger.view)})
            print(json.dumps(results[-1]), flush=True)
        finally:
            provider.close()
    target = attempts.GateTarget(INFERENCE_URL, model, selection.context_window_tokens,
        selection.context_reserve_tokens, selection.context_keep_recent_tokens, provider_id="cline",
        api_selection=selection.to_payload(), request_timeout=timeout)
    for case in cases:
        result = attempts.run_case_process(case, directory / case, target, timeout=timeout)
        results.append(result)
        print(json.dumps({"case": case, "ok": result.get("ok"), "failure_stage": result.get("failure_stage", "")}), flush=True)
    report = {"ok": all(row.get("ok") is True for row in results), "model": model, "results": results}
    attempts.write_json(directory / "summary.json", report)
    return report["ok"]


def reasoning_matrix(directory: Path, timeout: int) -> bool:
    resource = runtime()
    resource.catalog.refresh(force=True)
    directory.mkdir(parents=True, exist_ok=False)
    results = []
    for model in resource.payload()["models"]:
        choices: list[dict[str, object]] = [{"effort": effort} for effort in model["efforts"]]
        if model["thinking_toggle"]:
            choices = [{"thinking": value} for value in (None, True, False)]
        for choice in choices:
            row = {"model": model["id"], **choice, "ok": False}
            provider = None
            try:
                provider = open_selection(capture_selection({"model": model["id"], **choice}))
                provider.timeout = timeout
                text = provider.send("Reply with the single word READY.", timeout=timeout)
                row.update(ok=bool(text), history_items=len(provider.context_ledger.view))
            except (OSError, ValueError, RuntimeError) as exc:
                row["error"] = str(exc)
            finally:
                if provider is not None:
                    provider.close()
            results.append(row)
            print(json.dumps(row), flush=True)
            attempts.write_json(directory / "summary.json", {"ok": all(r["ok"] for r in results), "results": results})
    return all(row["ok"] for row in results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true", help="open device sign-in when not already connected")
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument("--reasoning-matrix", action="store_true")
    parser.add_argument("--model", default="cline-free/solar-mini4")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--cases", nargs="+", default=["chat", "read", "edit", "shell_approve"])
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if args.login:
        login()
    if args.login_only:
        return 0
    if args.run_dir is None:
        parser.error("--run-dir is required for live generation")
    if args.reasoning_matrix:
        return 0 if reasoning_matrix(args.run_dir, args.timeout) else 1
    return 0 if run(args.run_dir, args.model, args.cases, args.timeout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
