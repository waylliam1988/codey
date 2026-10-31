"""Optional raw-generation diagnostics must not change provider outcomes."""

from __future__ import annotations

import json
from http.client import HTTPException
from urllib.request import Request, urlopen


def record_native_generation(upstream, record):
    request = Request(upstream.rstrip("/") + "/api/extra/generate/check", b"{}",
                      {"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            body = json.load(response)
    except (OSError, HTTPException, ValueError) as exc:
        record({"event": "native_generation_diagnostic_failure", "error": repr(exc)})
    else:
        record({"event": "native_generation", "body": body})
