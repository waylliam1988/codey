"""Observe automatic cross-connection Review; unavailable is never a pass."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.providers.api_connections import capture_selection, open_selection
from codey.providers.api_provider import ApiProvider
from codey.providers.model_preferences import ModelPreferences
from codey.reviews.core import review_result_payload
from codey.reviews.identity import review_model_identity
from codey.reviews.persistence import load_recorded_review
from tests.manual.coding_pipeline_cases import BY_NAME
from tests.manual.coding_pipeline_value_ab import grade, save, write_files


def file_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()
            and not any(part in {"__pycache__", ".pytest_cache"} for part in path.parts)}


def verified(event):
    return (event.get("type") == "tool" and event.get("tool_name") == "run"
            and event.get("ok") is True and type(event.get("exit_code")) is int and event["exit_code"] == 0)


def gate_checks(payload):
    result, events = payload["result"], payload["events"]
    reviews = [(i, e["review"]) for i, e in enumerate(events)
               if e.get("type") == "review" and isinstance(e.get("review"), dict)]
    review_index, review = reviews[-1] if reviews else (-1, {})
    persisted = payload.get("persisted_review", {})
    terminals = [event for event in events if event.get("type") == "task_done"]
    terminal = terminals[0] if len(terminals) == 1 else {}
    metadata = ("verdict", "status", "origin", "attempt_id", "artifact_sha256")
    edits = [i for i, e in enumerate(events) if e.get("type") == "tool"
             and e.get("tool_name") == "edit" and e.get("ok") is True]
    last_edit = max(edits, default=-1)
    requests = payload.get("requests", [])
    expected = payload.get("expected_review_identity")
    review_requests = [r for r in requests if r["model"] == payload["reviewer_model"]]
    return {
        "writer_done": result.get("stop_reason") == "done" and type(result.get("exit_code")) is int
            and result["exit_code"] == 0,
        "single_terminal": len(terminals) == 1 and terminal.get("run_id") == result.get("run_id")
            and terminal.get("session_id") == result.get("session_id"),
        "real_writer_request": any(r["model"] == payload["writer_model"] for r in requests),
        "real_independent_review_request": bool(review_requests) and bool(expected)
            and all(r["model_identity"] == expected for r in review_requests)
            and payload["writer_model"] != payload["reviewer_model"],
        "complete_review": review.get("status") == "complete" and review.get("origin") == "fresh"
            and review.get("verdict") in {"approved", "changes_requested"},
        "persisted_independent_review": bool(expected) and persisted.get("self_review") is False
            and persisted.get("model_id") == expected and bool(persisted.get("artifact_sha256"))
            and all(persisted.get(k) == review.get(k) == terminal.get("review", {}).get(k) for k in metadata),
        "verified_before_review": review_index >= 0 and any(i < review_index for i in edits)
            and any(verified(e) for e in events[:review_index]),
        "terminal_after_review": len(terminals) == 1 and review_index >= 0 and events.index(terminal) > review_index,
        "fresh_final_verification": last_edit >= 0 and any(verified(e) for e in events[last_edit + 1:])
            and terminal.get("receipt", {}).get("verification", {}).get("checks_passed") is True,
        "review_read_only": bool(payload.get("review_snapshots"))
            and all(item["before"] == item["after"] for item in payload["review_snapshots"]),
        "independent_quality": bool(payload.get("quality", {}).get("quality_passed"))
            and payload.get("quality", {}).get("scope_ok") is True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewer-model", default="cline-free/solar-mini4")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    writer_model = capture_selection("local").model_id
    expected_provider = open_selection(capture_selection("cline", {"model": args.reviewer_model}))
    expected_identity = review_model_identity(expected_provider)
    expected_provider.close()
    with tempfile.TemporaryDirectory(prefix="codey-production-review-") as temporary:
        root, state = Path(temporary) / "project", Path(temporary) / "state"
        root.mkdir()
        case = BY_NAME["discount-bug"]
        write_files(root, case.files)
        prefs = ModelPreferences(state)
        sources = prefs.snapshot()["sources"]
        for source in sources.values():
            source.update(enabled=False, models=[])
        sources["local"] = {"enabled": True, "models": [writer_model]}
        sources["cline"] = {"enabled": True, "models": [args.reviewer_model]}
        prefs.save(sources, base_revision=prefs.snapshot()["revision"])
        events, requests, snapshots = [], [], []
        original = ApiProvider._observe_http_attempt
        before = None

        def observe(instance, **kwargs):
            nonlocal before
            original(instance, **kwargs)
            if kwargs["phase"] == "request":
                requests.append({"model": instance.model, "model_identity": review_model_identity(instance),
                                 "payload": json.loads(kwargs["data"])})
                if instance.model == args.reviewer_model:
                    before = file_hashes(root)
            elif instance.model == args.reviewer_model and before is not None:
                snapshots.append({"before": before, "after": file_hashes(root)})
                before = None

        # Observation only. Default headless factories choose both providers.
        with patch.object(ApiProvider, "_observe_http_attempt", observe):
            result = run_headless(
                HeadlessRequest(project=root, task=case.task, provider_id="local", model_selection={},
                                max_turns=24, state_home=state, project_changes_required=True),
                emit_jsonl=events.append,
            )
        persisted = load_recorded_review(state, result.session_id, result.run_id)
        persisted_payload = {}
        if persisted is not None:
            review = persisted[1]
            persisted_payload = {**review_result_payload(review), "self_review": review.identity.self_review,
                                 "model_id": review.identity.model_id, "reviewer_id": review.identity.reviewer_id}
        ledger = Path(result.ledger_path)
        payload = {
            "writer_model": writer_model, "reviewer_model": args.reviewer_model,
            "expected_review_identity": expected_identity, "result": result.__dict__,
            "events": events, "requests": requests, "review_snapshots": snapshots,
            "persisted_review": persisted_payload, "quality": grade(root, case),
            "ledger": ledger.read_text(encoding="utf-8") if ledger.is_file() else "",
        }
        payload["checks"] = gate_checks(payload)
        payload["ok"] = all(payload["checks"].values())
        save(args.output, payload)
        print(json.dumps({"ok": payload["ok"], "stop_reason": result.stop_reason,
                          "checks": payload["checks"]}, ensure_ascii=False))
        return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
