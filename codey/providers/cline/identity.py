"""Pinned Cline HTTP compatibility profile, reviewed against CLI 3.0.74."""
from __future__ import annotations

API_ORIGIN = "https://api.cline.bot"
INFERENCE_URL = API_ORIGIN + "/api/v1"
RECOMMENDED_URL = API_ORIGIN + "/api/v1/ai/cline/recommended-models"
CAPABILITIES_URL = "https://openrouter.ai/api/v1/models"
WORKOS_ORIGIN = "https://api.workos.com"
WORKOS_CLIENT_ID = "client_01K3A541FN8TA3EPPHTD2325AR"
CONNECTION_REVISION = "cline-http-v1"


def inference_api_key(raw: str) -> str:
    value = raw.strip()
    if not value or value == "workos:":
        raise ValueError("Cline access token is empty")
    return value if value.startswith("workos:") else "workos:" + value


def client_headers(task_id: str = "") -> dict[str, str]:
    return {"User-Agent": "Cline/3.0.74", "X-CLIENT-TYPE": "cline-cli", "X-CLIENT-VERSION": "3.0.74",
            "X-PLATFORM": "cli", "X-PLATFORM-VERSION": "3.0.74", "X-CORE-VERSION": "0.2.0",
            "HTTP-Referer": "https://cline.bot", "X-Title": "Cline", "X-IS-MULTIROOT": "false",
            **({"X-Task-ID": task_id} if task_id else {})}
