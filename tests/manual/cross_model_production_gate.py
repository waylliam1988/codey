"""Production headless smoke for local Writer + Cline API Reviewer."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.providers.model_preferences import ModelPreferences
from codey.reviews.core import review_result_payload
from codey.reviews.persistence import load_recorded_review

LOCAL_MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"
REVIEW_MODEL = "cline-free/nemotron-3.5-super-vl"


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "project"
        state = Path(temporary) / "state"
        root.mkdir()
        (root / "pricing.py").write_text(
            "def discounted_price(price, percent):\n    return price * (1 + percent / 100)\n",
            encoding="utf-8",
        )
        (root / "test_pricing.py").write_text(
            "from pricing import discounted_price\n\n"
            "def test_discount():\n    assert discounted_price(100, 20) == 80\n",
            encoding="utf-8",
        )
        (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        prefs = ModelPreferences(state)
        sources = prefs.snapshot()["sources"]
        sources["websites"] = {"enabled": False, "models": []}
        sources["local"] = {"enabled": True, "models": [LOCAL_MODEL]}
        sources["cline"] = {"enabled": True, "models": [REVIEW_MODEL]}
        sources["zen"] = {"enabled": False, "models": []}
        prefs.save(sources, base_revision=prefs.snapshot()["revision"])
        events: list[dict[str, object]] = []
        result = run_headless(
            HeadlessRequest(
                project=root,
                task=("Fix discounted_price in pricing.py: apply a percent discount, accept 0 and 100, "
                      "and raise ValueError for a percent below 0 or above 100. Keep its signature and tests unchanged."),
                provider_id="local",
                # Empty selection uses the current saved Local endpoint/model;
                # an API selection cannot be forged from a model ID alone.
                model_selection={},
                max_turns=18,
                state_home=state,
                project_changes_required=True,
            ),
            emit_jsonl=events.append,
        )
        check = subprocess.run(
            ["python", "-B", "-m", "pytest", "-q"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        review_events = [event for event in events if event.get("type") == "review"]
        persisted = load_recorded_review(state, result.session_id, result.run_id)
        persisted_payload: dict[str, object] = {}
        if persisted is not None:
            review = persisted[1]
            persisted_payload = {
                **review_result_payload(review),
                "self_review": review.identity.self_review,
                "model_id": review.identity.model_id,
                "reviewer_id": review.identity.reviewer_id,
            }
        payload = {
            "writer_model": LOCAL_MODEL,
            "reviewer_model": REVIEW_MODEL,
            "result": result.__dict__,
            "review_events": review_events,
            "persisted_review": persisted_payload,
            "pytest_ok": check.returncode == 0,
            "pytest_output": check.stdout + check.stderr,
            "changed_files": ["pricing.py"],
        }
        output = Path("tests/manual/results/cross-model-production-gate-20261106.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"stop_reason": result.stop_reason, "exit_code": result.exit_code,
                          "reviews": len(review_events), "pytest_ok": check.returncode == 0}, ensure_ascii=False))
        return 0 if result.exit_code == 0 and check.returncode == 0 and len(review_events) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
