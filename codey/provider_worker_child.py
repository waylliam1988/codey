"""Child process entry point for isolated Provider adapter overrides."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from codey.providers.registry import PROVIDER_TYPES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--port", type=int, required=True)
    # Required and fail-closed: the override worker must never attach to the
    # user's default browser profile from a second CDP port.
    parser.add_argument("--profile", required=True)
    args = parser.parse_args(argv)
    provider_type = PROVIDER_TYPES.get(args.provider)
    if provider_type is None:
        return 2
    try:
        provider = provider_type.connect(
            port=args.port,
            profile=Path(args.profile),
            open_if_missing=True,
            bring_to_front=False,
            isolated=False,
            fresh_tab=True,
        )
    except Exception as exc:
        _event("startup_error", error=f"{type(exc).__name__}: {exc}")
        raise
    _event("page", port=getattr(provider.session, "cdp_port", 0), target_id=_target_id(provider))
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(request, dict):
                continue
            request_id = str(request.get("id") or "")
            method = str(request.get("method") or "")
            params = request.get("params")
            params = params if isinstance(params, dict) else {}
            try:
                if method == "new_chat":
                    provider.new_chat(timeout=params.get("timeout"))
                    _reply(request_id, True, None)
                elif method == "send":
                    _reply(
                        request_id,
                        True,
                        provider.send(
                            str(params.get("text") or ""),
                            timeout=params.get("timeout"),
                        ),
                    )
                elif method == "close":
                    provider.close()
                    _reply(request_id, True, None)
                    return 0
                elif method == "health":
                    _reply(request_id, True, {"provider": args.provider})
                else:
                    _reply(request_id, False, error=f"unsupported method: {method}")
            except Exception as exc:
                failure = getattr(exc, "failure", None)
                payload = failure.to_dict() if hasattr(failure, "to_dict") else None
                _reply(request_id, False, error=str(exc), failure=payload)
    finally:
        try:
            provider.close()
        except Exception:
            pass
    return 0


def _target_id(provider) -> str:
    try:
        session = provider.session.page.context.new_cdp_session(provider.session.page)
        info = session.send("Target.getTargetInfo")
        target = info.get("targetInfo") if isinstance(info, dict) else None
        return str(target.get("targetId") or "") if isinstance(target, dict) else ""
    except Exception:
        return ""


def _event(name: str, **payload) -> None:
    data = {"event": name}
    data.update(payload)
    print(json.dumps(data, ensure_ascii=False, separators=(",", ":")), flush=True)


def _reply(
    request_id: str,
    ok: bool,
    result=None,
    *,
    error: str = "",
    failure: dict | None = None,
) -> None:
    payload = {"id": request_id, "ok": ok}
    if ok:
        payload["result"] = result
    else:
        payload["error"] = error
        if failure:
            payload["failure"] = failure
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
