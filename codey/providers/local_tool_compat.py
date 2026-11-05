"""Known local backend defects resolved before a task's protocol is frozen."""
from __future__ import annotations

import re
import urllib.parse


def safe_native_tools(base_url: str, model: str, *, api_protocol: str,
                      requested: bool, api_key: str = "") -> bool:
    """Use existing text tools for the reproduced KoboldCpp/Gemma parser hang.

    No generation is sent and no persisted preference is changed. Unknown
    backends, other models and other versions retain their selected protocol.
    """
    if (not requested or api_protocol != "openai-completions"
            or re.search(r"(?:^|[/_-])gemma[-_ ]?4(?=$|[/_-])", model, re.IGNORECASE) is None):
        return requested
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.path.rstrip("/") != "/v1":
        return requested
    from codey.providers.local_selection import _metadata_json

    root = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    version = _metadata_json(root + "/api/extra/version", api_key)
    affected = version.get("result") == "KoboldCpp" and version.get("version") == "1.117.1"
    return requested and not affected
