"""Ghost directive arm seeding via production Ghost stores.

Creates per-arm isolated state homes under arms/<ARM>/:
  A, B -> identical ghost data: 8 user-scope preferences, accepted through the
          real inbox gate + review + hebbian reinforce path, with affinity
          synced from the same hebbian graph.
  C    -> empty ghost (directive will render nothing).

Only real seconds are used for staggering (directive sort keys are whole
seconds), so each ingest/review/reinforce pair waits >2s. No DB values are
hand-edited; everything follows the public CLI/store path.

Usage:
  python seed_arms.py --endpoint http://127.0.0.1:5001/v1 --model "<model-id>"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from experiment_paths import DATA_ROOT, REPO

HERE = DATA_ROOT
ARMS_ROOT = HERE / "arms"
sys.path.insert(0, str(REPO))

SEED_SESSION = "ghost-ab-seed"

PREFS = [
    # (kind, evidence_quote, conflict_key_hint, value_key_hint)
    # Only typed-slot pairs in the fixture allowlist can enter the final prompt.
    # 7 distinct slots/values to keep ranking meaningful.
    ("style_preference", "Any answer should be within three sentences, unless I explicitly ask you to expand.",
     "reply_length", "brief"),
    ("style_preference", "When listing multiple points, answer with a numbered list.",
     "format", "bullets"),
    ("style_preference", "Start the answer with a one-sentence conclusion, then expand with the explanation.",
     "reply_structure", "answer_first_concise"),
    ("style_preference", "Use professional technical wording, don't be too colloquial.",
     "tone", "technical"),
    ("correction", "Persist the recorded state using JSONL, and copy the JSON projection as-is.",
     "state_store", "jsonl"),
    ("long_term_goal", "Keep the local memory always auditable, with no untraceable hidden state.",
     "ghost", "auditable"),
    ("style_preference", "Answer concisely; omit filler words.",
     "reply_length_2", "concise"),
    ("style_preference", "Use a previous-statistics table format when presenting comparison conclusions.",
     "format_2", "markdown"),
    # Extra typed slots so 10 renderable nodes exceed MAX_DIRECTIVE_ITEMS=8:
    # ranking must now decide the two dropped lines.
    ("correction", "Store the durable state backend as JSON projection plus JSONL audit.",
     "state_backend", "jsonl"),
    ("correction", "Keep the ghost state backend as JSON projection plus JSONL audit.",
     "ghost_state_backend", "json_projection_jsonl"),
    ("correction", "Keep the local memory state as a JSONL audit stream.",
     "memory_state", "jsonl"),
    ("long_term_goal", "Keep the memory state auditable and reviewable at all times.",
     "memory_state", "auditable"),
]

USER_TEXT = (
    "以下是我的长期偏好记录。"
    + " ".join(quote for _kind, quote, _slot, _value in PREFS)
)


def seed_arm(arm: str, endpoint: str, model: str) -> dict:
    from codey.ghost.affinity import GhostAffinityStore
    from codey.ghost.hebbian import GhostHebbianStore
    from codey.ghost.inbox import GhostInboxStore
    from codey.ghost.schema import GhostSignal
    from codey.providers import local_config

    arm_dir = ARMS_ROOT / arm
    arm_dir.mkdir(parents=True, exist_ok=True)

    # Local connection for this isolated state home (same as the gate tools).
    from unittest.mock import patch
    with patch.dict("os.environ", {"LOCAL_OPENAI_BASE_URL": endpoint, "LOCAL_OPENAI_MODEL": model}), patch.object(local_config, "DEFAULT_STATE_HOME", arm_dir):
        local_config.save_local_config(local_config.LocalProviderConfig(
            base_url=endpoint, model=model, api_protocol="openai-completions",
            native_tools_mode="auto", connection_revision="ghost-ab-seed",
            context=local_config.LocalContextBudget(262144, 32768, 32000)))

    if arm == "C":
        return {"arm": arm, "nodes": 0, "notes": "empty ghost"}

    inbox = GhostInboxStore(arm_dir)
    inbox.set_learning_enabled(True)

    accepted: list[object] = []
    for index, (kind, quote, slot, value) in enumerate(PREFS):
        summary = f"{slot.replace('_', ' ').title()} preference: {value}"
        decision = inbox.ingest_signals(
            (GhostSignal(
                kind=kind, scope="user", summary=summary, evidence_quote=quote,
                confidence=0.9, source="manual",
                metadata={"topic": f"ghost-ab-{index}",
                          "conflict_key": slot, "value_key": value}),),
            session_id=SEED_SESSION, run_id="",
        )
        if not decision:
            raise SystemExit(f"[{arm}] signal {index} rejected by gate: kind={kind}")
        candidate = review_accept(inbox, decision)
        accepted.append(candidate)
        time.sleep(2.3)

    hebbian = GhostHebbianStore(arm_dir)
    for candidate in accepted:
        related = [row for row in accepted if row is not candidate]
        result = hebbian.reinforce_candidate(candidate, related_candidates=related)
        if not result.applied:
            raise SystemExit(f"[{arm}] reinforce failed: {result.reason}")
        time.sleep(2.3)

    affinity = GhostAffinityStore(arm_dir)
    sync = affinity.sync_from_sources(hebbian_store=hebbian)
    nodes = hebbian.list_nodes()

    # Freeze experiment conditions across all arms.
    inbox.set_learning_enabled(False)

    directive_payload = dump_directive(arm_dir, expected_nodes=len(nodes))
    return {"arm": arm, "nodes": len(nodes), "sync_ok": bool(sync.ok), "directive": directive_payload}


def review_accept(inbox, candidates):
    target = candidates[0]
    reviewed = inbox.review_candidate(target.id, "accept", reviewed_by="ghost-ab")
    if reviewed is None or reviewed.status != "accepted":
        raise SystemExit(f"review failed for {target.id}")
    return reviewed


def dump_directive(arm_dir: Path, expected_nodes: int) -> dict:
    from codey.ghost.affinity import GhostAffinityStore
    from codey.ghost.directive import build_ghost_directive
    from codey.ghost.hebbian import GhostHebbianStore

    hebbian = GhostHebbianStore(arm_dir)
    nodes = hebbian.list_nodes()
    affinity = GhostAffinityStore(arm_dir)
    directive = build_ghost_directive(hebbian, affinity_store=affinity)
    payload = directive.to_payload()
    payload["node_count"] = len(nodes)
    payload["expected_node_count"] = expected_nodes
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    report = {}
    for arm in ("A", "B", "C"):
        report[arm] = seed_arm(arm, args.endpoint, args.model)
        print(json.dumps(report[arm], ensure_ascii=False))

    directives = {arm: value.get("directive") or {} for arm, value in report.items()}
    (HERE / "directives.json").write_text(
        json.dumps(directives, ensure_ascii=False, indent=2), encoding="utf-8")
    for arm in ("A", "B"):
        text = (directives[arm] or {}).get("text", "")
        (HERE / f"directive_{arm}.txt").write_text(text, encoding="utf-8")
    (HERE / "directive_C.txt").write_text("", encoding="utf-8")

    a_text, b_text = (directives.get("A") or {}).get("text", ""), (directives.get("B") or {}).get("text", "")
    summary = {
        "identical_text": a_text == b_text,
        "a_bytes": len(a_text.encode("utf-8")),
        "b_bytes": len(b_text.encode("utf-8")),
        "a_selected": (directives.get("A") or {}).get("selected_count"),
        "b_selected": (directives.get("B") or {}).get("selected_count"),
    }
    (HERE / "seed_report.json").write_text(
        json.dumps({"arms": report, "text_compare": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
