"""Manual A/B for Verification Map influence on live web reviewers."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

# ruff: noqa: E402 - direct script execution must add the repository root first.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey.providers.registry import connect_provider, provider_ids
from codey.review import parse_review_with_repair, render_review_prompt

OUTPUT = Path(tempfile.gettempdir()) / "codey-verification-review-ab.json"
CHANGES = {
    "ok": True,
    "changed_count": 1,
    "files": [{"path": "src/auth.py", "status": "M", "additions": 4, "deletions": 1}],
    "diff": (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "--- a/src/auth.py\n+++ b/src/auth.py\n"
        "@@\n-def normalize_username(value):\n-    return value\n"
        "+def normalize_username(value):\n"
        "+    if not value.strip():\n+        raise ValueError('username required')\n"
        "+    return value.strip().lower()\n"
    ),
}
VERIFICATION_MAP = """Verification Map (bounded candidates; not coverage proof):
Changed files:
- src/auth.py
Changed tests:
- (none)
Existing test candidates found locally (not necessarily changed):
- tests/test_auth.py: name corresponds to changed file src/auth.py [evidence: naming]
Observed successful checks after the latest edit:
- (none observed)
Broader check candidates (inspect relevance before requesting):
- python -m pytest [evidence: project manifest]
"""


def run(provider_id: str, arm: str, port: int) -> dict[str, object]:
    provider = connect_provider(provider_id, port=port, open_if_missing=False)
    started = time.monotonic()
    try:
        provider.new_chat()
        prompt = render_review_prompt(
            project="temporary-auth-project",
            task=(
                "Make normalize_username reject blank input and return a stripped, "
                "lowercase username. Keep existing callers working and verify the change."
            ),
            writer_summary="Implemented blank validation and normalization in src/auth.py.",
            changes=CHANGES,
            recent_log="[agent] edit src/auth.py succeeded; no run result followed",
            verification_map=VERIFICATION_MAP if arm == "current" else "",
        )
        reply = provider.send(prompt)
        parsed = parse_review_with_repair(reply, provider.send)
        return {
            "provider": provider_id,
            "arm": arm,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "verdict": parsed.verdict,
            "summary": parsed.summary,
            "findings": [
                {"path": item.path, "issue": item.issue, "suggested_fix": item.suggested_fix}
                for item in parsed.findings
            ],
            "mentions_existing_test": "test_auth.py" in reply,
            "mentions_check": any(term in reply.lower() for term in ("pytest", "test", "check")),
        }
    finally:
        provider.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=provider_ids(), required=True)
    parser.add_argument("--arm", choices=("baseline", "current"), required=True)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    row = run(args.provider, args.arm, args.port)
    rows = []
    if args.output.exists():
        try:
            rows = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
    args.output.write_text(json.dumps([*rows, row], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
