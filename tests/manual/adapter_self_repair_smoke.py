"""Live smoke for Provider adapter self-repair workers.

The smoke uses product-neutral marker prompts and records only bounded status
metadata. It does not install an adapter override; the candidate canary path is
exercised with the current source tree as the candidate root.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey.adapter_overrides import AdapterOverride  # noqa: E402
from codey.adapter_repair import run_worker_canary  # noqa: E402
from codey.local_store import DEFAULT_STATE_HOME  # noqa: E402
from codey.self_repair_worker import connect_repair_helper  # noqa: E402


def run_smoke(provider: str, *, timeout: float, state_home: Path) -> dict:
    provider = provider.strip().lower()
    result = {
        "provider": provider,
        "state_home": str(state_home),
        "started_at": time.time(),
    }
    result["fresh_helper"] = _smoke_fresh_helper(provider, timeout)
    result["candidate_worker_canary"] = _smoke_candidate_canary(provider, timeout, state_home)
    result["ok"] = bool(
        result["fresh_helper"].get("ok")
        and result["candidate_worker_canary"].get("ok")
    )
    result["duration_seconds"] = round(time.time() - float(result["started_at"]), 3)
    return result


def _smoke_fresh_helper(provider: str, timeout: float) -> dict:
    marker = "SESSION_CHECK_" + secrets.token_hex(6).upper()
    helper = None
    started = time.time()
    try:
        helper = connect_repair_helper(provider)
        helper.new_chat(timeout=timeout)
        reply = helper.send(
            f"Return exactly this marker and nothing else: {marker}",
            timeout=timeout,
        )
        text = str(reply or "").strip()
        return {
            "ok": text == marker,
            "duration_seconds": round(time.time() - started, 3),
            "reply_chars": len(text),
            "exact_marker": text == marker,
        }
    except BaseException as exc:
        return {
            "ok": False,
            "duration_seconds": round(time.time() - started, 3),
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
    finally:
        if helper is not None:
            try:
                helper.close()
            except BaseException:
                pass


def _smoke_candidate_canary(provider: str, timeout: float, state_home: Path) -> dict:
    generation = 900_000 + secrets.randbelow(90_000)
    started = time.time()
    try:
        override = AdapterOverride(
            provider_id=provider,
            generation=generation,
            status="candidate",
            root=ROOT,
        )
        ok = run_worker_canary(
            provider,
            override,
            state_home=state_home,
            attempts=1,
            timeout=timeout,
        )
        return {
            "ok": bool(ok),
            "duration_seconds": round(time.time() - started, 3),
            "generation": generation,
        }
    except BaseException as exc:
        return {
            "ok": False,
            "duration_seconds": round(time.time() - started, 3),
            "generation": generation,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="qwen")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--state-home", type=Path, default=DEFAULT_STATE_HOME)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    report = run_smoke(
        args.provider,
        timeout=max(1.0, args.timeout),
        state_home=args.state_home,
    )
    output = args.output or Path(tempfile.gettempdir()) / (
        f"codey-adapter-self-repair-smoke-{args.provider}.json"
    )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"[adapter-self-repair-smoke] report: {output}", flush=True)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
