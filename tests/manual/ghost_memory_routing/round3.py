"""Round 3: tiered-benefit ranking ablation + conflict-supersede wire test.

Tests the 5-tier design, harness-only (no production edits):
  D2 = tiered-benefit ranking:
        benefit = tier prior (+0.75 task-boost for tier-2 explicit-key matches)
        tier1 user prefs > tier2 task constraints > tier3 facts > tier4 self-desc
        (tier4 prior 0.0 -> delivered only if the 8-item budget is free)
  T5 = conflict wire test: same slot (language) seeded chinese then english;
        production supersede must keep exactly one language node.
Baselines A2/B2/C2 reuse round-2 runs (same fixtures, same battery).
"""
# ruff: noqa: E402 -- direct-script bootstrap before importing Codey.
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from contextlib import contextmanager
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO, SCRIPT_DIR

HERE = DATA_ROOT
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCRIPT_DIR))

import round2  # brings round1 helpers + expanded renderer
import run_arm as round1
from run_arm import GateHTTPClient, judge_answer

import codey.ghost._common as gcommon
import codey.ghost.directive as gdir

RUNS3_DIR = HERE / "runs3"

CURRENT_TASK = ""  # harness-level task text for tier-2 boosting

TIER_TABLE = {  # (kind, slot) -> (tier, prior, trigger_regex)
    ("style_preference", "language"): (1, 0.90, None),
    ("style_preference", "code_notes"): (1, 0.80, None),
    ("style_preference", "reply_structure"): (1, 0.50, None),
    ("style_preference", "reply_length"): (1, 0.40, None),
    ("style_preference", "format"): (1, 0.30, None),      # baseline-saturated penalty
    ("style_preference", "tone"): (1, 0.25, None),
    ("action_tendency", "test_command"): (2, 0.30, r"测试|pytest|跑"),
    ("action_tendency", "commit_flow"): (2, 0.30, r"提交|commit"),
    ("research_interest", "focus"): (3, 0.50, None),
    ("correction", "state_store"): (4, 0.0, None),
    ("correction", "state_backend"): (4, 0.0, None),
    ("correction", "ghost_state_backend"): (4, 0.0, None),
    ("correction", "memory_state"): (4, 0.0, None),
    ("long_term_goal", "ghost"): (4, 0.0, None),
}

TASKS = round2.TASKS
DECISIVE = round2.DECISIVE


def slot_of(kind: str, conflict_key: str) -> str:
    prefix = f"{kind}:"
    return conflict_key[len(prefix):] if (conflict_key or "").startswith(prefix) else (conflict_key or "")


def tiered_sort_key(node, hint_weights=None):
    tier, prior, trigger = TIER_TABLE.get(
        (node.kind, slot_of(node.kind, node.conflict_key)), (3, 0.4, None))
    boost = 0.75 if (trigger and CURRENT_TASK and re.search(trigger, CURRENT_TASK)) else 0.0
    return (-(prior + boost), tier, gcommon.reverse_text_sort_key(node.updated_at))


@contextmanager
def tier_patch_context():
    try:
        gdir.render_typed_field = round2.expanded_render
        gdir._node_sort_key = tiered_sort_key
        yield
    finally:
        gdir.render_typed_field = round2.ORIG_RENDER
        gdir._node_sort_key = round2.ORIG_SORT_KEY


# ------------------------------------------------------------------ tests
def seed_t5() -> dict:
    """Tier-5 wire test: same slot, changed preference; supersede must fire."""
    from time import sleep

    from codey.ghost.hebbian import GhostHebbianStore
    from codey.ghost.inbox import GhostInboxStore
    from codey.ghost.schema import GhostSignal

    arm_dir = round2.ARMS_ROOT / "T5"
    if arm_dir.exists():
        raise SystemExit(f"T5 state home already exists: {arm_dir}")
    arm_dir.mkdir(parents=True)

    inbox = GhostInboxStore(arm_dir)
    inbox.set_learning_enabled(True)
    hebbian = GhostHebbianStore(arm_dir)

    def push(summary: str, quote: str, value: str) -> None:
        decision = inbox.ingest_signals(
            (GhostSignal(kind="style_preference", scope="user", summary=summary,
                         evidence_quote=quote, confidence=0.9, source="manual",
                         metadata={"conflict_key": "language", "value_key": value}),),
            session_id="ghost-t5", run_id="")
        target = decision[0]
        if getattr(target, "status", "") != "accepted":
            target = inbox.review_candidate(target.id, "accept", reviewed_by="ghost-t5")
        result = hebbian.reinforce_candidate(target, related_candidates=(target,))
        if not result.applied:
            raise SystemExit(f"T5 reinforce not applied ({value}): {result.reason}")
        sleep(2.3)

    push("language confirmed once (chinese)",
         "技术解释一律用中文写。", "chinese")
    push("language changed later (english)",
         "Switch technical explanations to English going forward.", "english")

    nodes = [n for n in hebbian.list_nodes() if "language" in (n.conflict_key or "")]
    with tier_patch_context():
        payload = round2.dump_directive("T5")
    text = payload.get("text", "")
    has_chinese = "Chinese" in text
    has_english = "English" in text
    inbox.set_learning_enabled(False)
    return {"nodes_language": len(nodes), "has_chinese": has_chinese,
            "has_english": has_english,
            "supersede_ok": len(nodes) == 1 and has_english and not has_chinese,
            "directive_text": text}


def family_membership(text: str) -> dict:
    return {
        "language": "reply language" in text,
        "tool_command": "test command" in text or "commit workflow" in text,
        "format": "format = bullets" in text,
        "code_constraints": "code notes" in text,
        "self_desc_lines": sum(1 for ln in text.splitlines()
                               if "state" in ln.lower() or "memory" in ln.lower()),
    }


# ------------------------------------------------------------------ runner
def run_d2(reps: int, endpoint: str, model: str, timeout_run: float) -> int:
    RUNS3_DIR.mkdir(exist_ok=True)
    arm = "D2"
    state_root = round2.ARMS_ROOT / arm
    rows_path = RUNS3_DIR / f"{arm}.jsonl"
    if rows_path.exists():
        rows_path.unlink()

    from codey.app import server as app_server
    from codey.app.context import AppContext
    from codey.providers import local_config

    events: list[dict] = []
    with patch.dict("os.environ", {"NATIVE_TOOLS": "1"}), \
            patch.object(local_config, "DEFAULT_STATE_HOME", state_root), \
            tier_patch_context():
        local_config.save_local_config(local_config.LocalProviderConfig(
            base_url=endpoint, model=model, api_protocol="openai-completions",
            native_tools_mode="auto", connection_revision="ghost-ab3-run",
            context=local_config.LocalContextBudget(262144, 32768, 32000)))
        state = AppContext(state_root)
        try:
            state._on_event_emitted = lambda row: events.append(dict(row))
            snapshot = state.providers.model_preferences.snapshot()
            sources = snapshot["sources"]
            sources["local"] = {"enabled": True, "models": [model]}
            state.providers.model_preferences.save(sources, base_revision=snapshot["revision"])
            state.ghost_inbox.set_learning_enabled(True)

            with patch.object(app_server, "STATE", state):
                httpd = app_server.CodeyHTTPServer(("127.0.0.1", 0), app_server.Handler)
                thread = round1.threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    client = GateHTTPClient(httpd)
                    with rows_path.open("a", encoding="utf-8") as sink:
                        for task in TASKS:
                            for rep in range(reps):
                                CURRENT_TASK = task["text"]  # tier-2 boost signal
                                globals()["CURRENT_TASK"] = CURRENT_TASK
                                session_id = f"ghostab3-{task['id']}-r{rep}-{int(time.time()*1000)}"
                                submitted = client.post("/api/run", {
                                    "session_id": session_id,
                                    "input": {"kind": "start_task", "text": task["text"]},
                                    "provider": "local",
                                    "model_selection": {"model": model, "base_url": endpoint},
                                    "intent": "chat", "max_turns": 4})
                                run_id = submitted["run_id"]
                                terminal, deadline = {}, time.monotonic() + timeout_run
                                events.clear()
                                while time.monotonic() < deadline:
                                    snap = client.state()
                                    event = snap.get("last_terminal_event") or {}
                                    if event.get("run_id") == run_id and not snap.get("busy"):
                                        terminal = event
                                        break
                                    time.sleep(0.05)
                                stop_reason = terminal.get("stop_reason") or {}
                                if isinstance(stop_reason, dict):
                                    stop_reason = stop_reason.get("value")
                                text = terminal.get("summary") or ""
                                if stop_reason != "done":
                                    text = text or round1.final_answer_text(state, run_id, events)
                                judgement = judge_answer(task["judge"], text) if text else {
                                    "judge": task["judge"], "passed": False, "detail": "no_text"}
                                row = {
                                    "arm": arm, "task": task["id"], "rep": rep,
                                    "session_id": session_id, "run_id": run_id,
                                    "stop_reason": stop_reason, "chars": len(text),
                                    "seconds": round(min(time.monotonic(), deadline) - (deadline - timeout_run), 1),
                                    **{f"judge_{k}": v for k, v in judgement.items()},
                                    "text": text,
                                }
                                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                                sink.flush()
                                print(json.dumps({k: row[k] for k in
                                                  ("arm", "task", "rep", "stop_reason", "chars",
                                                   "judge_passed", "judge_detail")}, ensure_ascii=False))
                finally:
                    state.request_stop()
                    deadline = time.monotonic() + 10
                    while state.is_busy() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)
        finally:
            state.close()
    return 0


# ------------------------------------------------------------------ report
def build_report() -> dict:
    arms = {}
    for directory, _arm_filter in ((round2.RUNS_DIR, None), (RUNS3_DIR, None)):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            arm = path.stem
            table: dict[str, dict[str, float]] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                cell = table.setdefault(row["task"], {"runs": 0, "passed": 0, "chars": 0.0})
                cell["runs"] += 1
                cell["chars"] += float(row.get("chars") or 0)
                if row.get("judge_passed"):
                    cell["passed"] += 1
            arms[arm] = table

    lines = ["# Ghost round-3: tiered routing ablation", ""]
    order = ["D2", "A2", "B2", "C2"]
    for arm in order:
        table = arms.get(arm)
        if not table:
            continue
        decisive_runs = sum(table[t]["runs"] for t in DECISIVE if t in table)
        decisive_pass = sum(table[t]["passed"] for t in DECISIVE if t in table)
        lines.append(f"## {arm} (decisive {decisive_pass:.0f}/{decisive_runs:.0f})")
        for task, cell in sorted(table.items()):
            avg = cell["chars"] / max(1, cell["runs"])
            lines.append(f"- {task}: {cell['passed']:.0f}/{cell['runs']:.0f} (chars_avg={avg:.1f})")
        lines.append("")

    directives = {}
    for arm in order:
        path = HERE / f"directive_{arm}.txt"
        if path.exists():
            directives[arm] = family_membership(path.read_text(encoding="utf-8"))
    (HERE / "REPORT3.md").write_text("\n".join(lines), encoding="utf-8")
    return {"table": lines, "families": directives}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed-d2", "t5", "preview", "run", "report"])
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", default="koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M")
    parser.add_argument("--timeout-run", type=float, default=120.0)
    args = parser.parse_args()
    if args.command == "seed-d2":
        result = round2.seed_arm("D2", args.endpoint, args.model)
        with tier_patch_context():
            preview = round2.dump_directive("D2")
        (HERE / "directive_D2.txt").write_text(preview.get("text", ""), encoding="utf-8")
        print(json.dumps({"arm": "D2", "nodes": result.get("nodes"),
                          "selected": preview.get("selected_count"),
                          "bytes": len(preview.get("text", "").encode("utf-8"))},
                         ensure_ascii=False))
        return 0
    if args.command == "t5":
        result = seed_t5()
        result.pop("directive_text", None)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "preview":
        with tier_patch_context():
            preview = round2.dump_directive("D2")
        (HERE / "directive_D2.txt").write_text(preview.get("text", ""), encoding="utf-8")
        print(json.dumps({k: preview.get(k) for k in ("selected_count", "truncated")},
                         ensure_ascii=False))
        return 0
    if args.command == "run":
        return run_d2(args.reps, args.endpoint, args.model, args.timeout_run)
    if args.command == "report":
        report = build_report()
        for arm, families in report["families"].items():
            print(arm, json.dumps(families, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
