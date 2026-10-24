"""One execution-host clock observation for an outgoing generation request."""
from __future__ import annotations

import json
from datetime import UTC, datetime


def clock_context() -> str:
    now = datetime.now().astimezone()
    data = {
        "observed_at_utc": now.astimezone(UTC).isoformat(timespec="seconds"),
        "local_datetime": now.isoformat(timespec="seconds"),
        "local_date": now.date().isoformat(),
        "utc_offset": now.strftime("%z"),
        "timezone_name": now.tzname(),
        "timezone_source": "execution_host_operating_system",
    }
    return (
        "<request_context>\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        + "\nThis clock observation is not source evidence. The user's timezone may differ; "
        "use an explicitly requested timezone for relative dates. Preserve dates in historical input.\n"
        "</request_context>"
    )
