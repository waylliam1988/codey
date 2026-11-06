"""Owned, bounded cancellation probes; never abort an unidentified generation."""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from codey.providers.api_provider import ApiProvider
from tests.manual.coding_pipeline_value_ab import save


def read(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:5001")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--admitted", action="store_true", help="probe production Local admission instead of explicit transports")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    model = read(args.base, "/v1/models")["data"][0]["id"]
    payload = {"model": model, "version": read(args.base, "/api/extra/version"), "rows": []}
    save(args.output, payload)
    for stream in ((None,) if args.admitted else (False, True)):
        before = read(args.base, "/api/extra/perf")
        if not before["idle"] or before["queue"]:
            raise RuntimeError("backend must be idle before each owned probe")
        key = "codey-timeout-diagnostic-" + uuid4().hex
        requests = []
        def configure(body, generation_key=key, captured=requests):
            body["genkey"] = generation_key
            captured.append(dict(body))
        if args.admitted:
            from codey.providers.local_connection import capture_selection, open_selection
            selection = capture_selection()
            client = open_selection(replace(selection, output_tokens=min(4096, selection.context_reserve_tokens)))
            if client.base_url != args.base + "/v1" or client.model != model:
                client.close()
                raise ValueError("admitted Local target differs from diagnostic target")
            original_configure = client.configure_request
            def admitted_configure(body, usage_configure=original_configure, key_configure=configure):
                usage_configure(body)
                key_configure(body)
            client.configure_request = admitted_configure
            client.timeout = 2
        else:
            client = ApiProvider(args.base + "/v1", model, timeout=2, stream=stream,
                                 native_tools=False, output_tokens=4096, thinking_enabled=False,
                                 configure_request=configure)
        row = {"stream": client.stream, "production_admission": args.admitted,
               "native_tools": client.native_tools, "text_tool_format": client.text_tool_format,
               "before": before, "requests": requests}
        started = time.monotonic()
        try:
            row["reply"] = client.send("Output the integers from 1 to 5000, one per line, without skipping or stopping early.")
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            row["client_seconds"] = round(time.monotonic() - started, 3)
            client.close()
        row["after"] = []
        for _ in range(4):
            time.sleep(1)
            row["after"].append(read(args.base, "/api/extra/perf"))
        if not row["after"][-1]["idle"]:
            # The unique key is preserved by KoboldCpp's OpenAI adapter.
            # A nonmatching key cannot abort another user's active request.
            row["owned_abort"] = read(args.base, "/api/extra/abort", {"genkey": key})
            time.sleep(1)
        row["settled"] = read(args.base, "/api/extra/perf")
        payload["rows"].append(row)
        save(args.output, payload)
        print(json.dumps({k: v for k, v in row.items() if k not in {"requests", "reply", "before", "after"}}), flush=True)
        if not row["settled"]["idle"] or row["settled"]["queue"]:
            raise RuntimeError("owned probe did not settle; do not queue another generation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
