"""Ghost directive A/B round 2: expanded slots + item-cap pressure.

Round-1 finding: 900-char budget never overflowed, so hebbian+affinity ranking
and naive recency produced byte-identical prompts (ranking untestable).

Round 2 changes the conditions:
  * 14 renderable preference nodes >= MAX_DIRECTIVE_ITEMS(8): ranking decides
    membership, not just order.
  * heavy nodes (production slots) get TWO confirmation cycles -> evidence
    stacking -> weight ~0.45; light nodes (expanded slots) one cycle -> ~0.225.
    Light nodes are confirmed LAST (newest) -> recency and weight disagree,
    so arm A2 (recency) and arm B2 (weight) must select different lines.
  * harness-level renderer expansion (no production edits) adds:
    language=chinese, test_command=pytest -q, commit_flow, code_notes, focus.

Arms: A2 = ranking off (pure recency), B2 = production ranking, C2 = none.
"""
# ruff: noqa: E402 -- direct-script bootstrap before importing Codey.
from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO, SCRIPT_DIR

HERE = DATA_ROOT
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCRIPT_DIR))

import run_arm as round1  # reuse HTTP client + judges
from run_arm import GateHTTPClient, judge_answer

ARMS_ROOT = HERE / "arms2"
RUNS_DIR = HERE / "runs2"
SEED_SESSION = "ghost-ab2-seed"

# (kind, slot, value, evidence_en, evidence_zh)
HEAVY = [
    ("style_preference", "reply_length", "brief",
     "Keep answers within three sentences unless I ask to expand.",
     "回答控制在三句以内，除非我明确要求展开。"),
    ("style_preference", "format", "bullets",
     "Multiple points are listed with bullet markers.",
     "要列多个要点时，用条目列出。"),
    ("style_preference", "reply_structure", "answer_first_concise",
     "Lead with a one-sentence conclusion, then explain.",
     "先给一句结论，再展开解释。"),
    ("style_preference", "tone", "technical",
     "Use technical wording, not chatty phrasing.",
     "用技术性措辞，不要太口语。"),
    ("correction", "state_store", "jsonl",
     "Persist recorded state as JSONL.",
     "记录状态用 JSONL 持久化。"),
    ("long_term_goal", "ghost", "auditable",
     "Local memory must stay auditable.",
     "本地记忆必须始终可审计。"),
]
LIGHT = [
    ("correction", "state_backend", "jsonl",
     "Store the durable state backend as JSONL.",
     "持久层状态存 JSONL。"),
    ("correction", "ghost_state_backend", "json_projection_jsonl",
     "The ghost state backend keeps JSON projection plus JSONL.",
     "ghost 状态后端保持 JSON 投影加 JSONL。"),
    ("style_preference", "language", "chinese",
     "Technical explanations are written in Chinese.",
     "技术解释一律用中文写。"),
    ("action_tendency", "test_command", "pytest -q",
     "Project tests run with pytest -q.",
     "项目测试统一用 pytest -q 跑。"),
    ("action_tendency", "commit_flow", "review_diff_first",
     "Review the git diff before committing.",
     "提交前先审查 git diff。"),
    ("style_preference", "code_notes", "minimal_prose",
     "Code answers carry minimal prose.",
     "代码回答少解释，给紧凑代码。"),
    ("research_interest", "focus", "concurrency_memory",
     "Long-term focus is concurrency and durable local memory.",
     "长期关注并发与持久本地记忆。"),
    ("correction", "memory_state", "jsonl",
     "The local memory state is a JSONL audit stream.",
     "本地记忆状态是 JSONL 审计流。"),
]

EXPANDED_LABELS = {
    ("style_preference", "language"): "reply language",
    ("action_tendency", "test_command"): "test command",
    ("action_tendency", "commit_flow"): "commit workflow",
    ("style_preference", "code_notes"): "code notes",
    ("research_interest", "focus"): "long-term focus",
}
EXPANDED_VALUES = {
    "pytest_q": "pytest -q",
    "review_diff_first": "review diff first",
    "chinese": "Chinese",
    "minimal_prose": "minimal prose, compact code",
    "concurrency_memory": "concurrency and durable local memory",
}

TASKS = [
    {"id": "t0_control", "text": "What is 17 multiplied by 24? Reply with only the number.",
     "judge": "contains_408"},
    {"id": "t1_chinese", "text": "Explain the CAP theorem in plain words.",
     "judge": "cjk_dominant"},
    {"id": "t2_short", "text": "说明什么是数据库索引。",
     "judge": "short_answer"},
    {"id": "t3_list", "text": "List the benefits of unit tests.",
     "judge": "numbered_list"},
    {"id": "t4_pytest", "text": "这个项目里平时跑测试用什么命令？直接回答就好，不要执行任何命令。",
     "judge": "pytest_q"},
    {"id": "t5_answer_first", "text": "什么是死锁？请尽量直接回答。",
     "judge": "answer_first"},
]
DECISIVE = ["t1_chinese", "t2_short", "t3_list", "t4_pytest", "t5_answer_first"]


# ------------------------------------------------------------ renderer patch
import codey.ghost._common as gcommon  # noqa: E402
import codey.ghost.directive as gdir  # noqa: E402

ORIG_RENDER = gdir.render_typed_field
ORIG_SORT_KEY = gdir._node_sort_key


def expanded_render(kind, conflict_key, value_key, max_chars=None):
    prefix = f"{kind}:"
    slot = conflict_key[len(prefix):] if (conflict_key or "").startswith(prefix) else (conflict_key or "")
    if (kind, slot) in EXPANDED_LABELS:
        human = EXPANDED_VALUES.get(value_key or "", (value_key or "").replace("_", " "))
        body = f"{EXPANDED_LABELS[(kind, slot)]} = {human}"
        return body[:max_chars] if max_chars else body
    if max_chars is not None:
        return ORIG_RENDER(kind, conflict_key, value_key, max_chars=max_chars)
    return ORIG_RENDER(kind, conflict_key, value_key)


def recency_sort_key(node, hint_weights=None):
    return (
        gdir.KIND_PRIORITY.get(node.kind, 99),
        gdir.SCOPE_PRIORITY.get(node.scope, 99),
        0.0,
        gcommon.reverse_text_sort_key(node.updated_at),
    )


@contextmanager
def arm_patch_context(arm: str):
    try:
        if arm != "C2":
            gdir.render_typed_field = expanded_render
        if arm == "A2":
            gdir._node_sort_key = recency_sort_key
        yield
    finally:
        gdir.render_typed_field = ORIG_RENDER
        gdir._node_sort_key = ORIG_SORT_KEY


# ---------------------------------------------------------------- seeding
def _signal(kind, slot, value, summary, quote, index):
    from codey.ghost.schema import GhostSignal
    return GhostSignal(
        kind=kind, scope="user", summary=summary, evidence_quote=quote,
        confidence=0.9, source="manual",
        metadata={"topic": f"ghost-ab2-{index}",
                  "conflict_key": slot, "value_key": value})


def seed_arm(arm: str, endpoint: str, model: str) -> dict:
    from codey.ghost.affinity import GhostAffinityStore
    from codey.ghost.hebbian import GhostHebbianStore
    from codey.ghost.inbox import GhostInboxStore
    from codey.providers import local_config

    arm_dir = ARMS_ROOT / arm
    if arm_dir.exists():
        raise SystemExit(f"[{arm}] state home already exists: {arm_dir}")
    arm_dir.mkdir(parents=True)

    with patch.dict("os.environ", {"LOCAL_OPENAI_BASE_URL": endpoint, "LOCAL_OPENAI_MODEL": model}), patch.object(local_config, "DEFAULT_STATE_HOME", arm_dir):
        local_config.save_local_config(local_config.LocalProviderConfig(
            base_url=endpoint, model=model, api_protocol="openai-completions",
            native_tools_mode="auto", connection_revision="ghost-ab2-seed",
            context=local_config.LocalContextBudget(262144, 32768, 32000)))

    if arm == "C2":
        return {"arm": arm, "nodes": 0, "notes": "empty ghost"}

    inbox = GhostInboxStore(arm_dir)
    inbox.set_learning_enabled(True)
    hebbian = GhostHebbianStore(arm_dir)
    evidence_log: list[tuple[str, str]] = []

    def cycle(kind_slot_value, summary, quote, index):
        kind, slot, value = kind_slot_value
        decision = inbox.ingest_signals(
            (_signal(kind, slot, value, summary, quote, index),),
            session_id=SEED_SESSION, run_id="")
        if not decision:
            raise SystemExit(f"[{arm}] gate rejected: kind={kind} slot={slot} i={index}: {decision}")
        target = decision[0]
        if getattr(target, "status", "") != "accepted":
            reviewed = inbox.review_candidate(target.id, "accept", reviewed_by="ghost-ab2")
            if reviewed is None or reviewed.status != "accepted":
                raise SystemExit(f"[{arm}] review failed: {target.id}")
            target = reviewed
        result = hebbian.reinforce_candidate(target, related_candidates=(target,))
        if not result.applied:
            raise SystemExit(f"[{arm}] reinforce not applied i={index}: {result.reason}")
        node_id = getattr(result, "node_id", "") or ""
        evidence_log.append((slot, value, node_id))
        time.sleep(2.3)

    for index, row in enumerate(HEAVY):  # phase 1: heavy first confirm
        kind, slot, value, ev, _evzh = row
        cycle((kind, slot, value), f"{slot} confirmed once ({value})", ev, index)
    for index, row in enumerate(HEAVY):  # phase 2: heavy re-confirmation (new evidence ref)
        kind, slot, value, _ev, evzh = row
        cycle((kind, slot, value), f"{slot} confirmed again ({value})", evzh, 100 + index)
    for index, row in enumerate(LIGHT):  # phase 3: light single confirm, newest timestamps
        kind, slot, value, ev, _evzh = row
        cycle((kind, slot, value), f"{slot} noted ({value})", ev, 200 + index)

    affinity = GhostAffinityStore(arm_dir)
    sync = affinity.sync_from_sources(hebbian_store=hebbian)
    nodes = hebbian.list_nodes()
    weights = {node.conflict_key: round(node.weight, 3) for node in nodes}
    inbox.set_learning_enabled(False)

    with arm_patch_context(arm):
        preview = dump_directive(arm)
    return {"arm": arm, "nodes": len(nodes), "sync_ok": bool(sync.ok),
            "weights": weights, "directive": preview}


def dump_directive(arm: str) -> dict:
    from codey.ghost.affinity import GhostAffinityStore
    from codey.ghost.directive import build_ghost_directive
    from codey.ghost.hebbian import GhostHebbianStore

    arm_dir = ARMS_ROOT / arm
    hebbian = GhostHebbianStore(arm_dir)
    affinity = GhostAffinityStore(arm_dir)
    directive = build_ghost_directive(hebbian, affinity_store=affinity)
    payload = directive.to_payload()
    payload["node_count"] = len(hebbian.list_nodes())
    return payload


# ---------------------------------------------------------------- runner
def run_arm(arm: str, reps: int, endpoint: str, model: str, timeout_run: float) -> int:
    RUNS_DIR.mkdir(exist_ok=True)
    state_root = ARMS_ROOT / arm
    rows_path = RUNS_DIR / f"{arm}.jsonl"
    if rows_path.exists():
        rows_path.unlink()

    from codey.app import server as app_server
    from codey.app.context import AppContext
    from codey.providers import local_config

    events: list[dict] = []
    with patch.dict("os.environ", {"NATIVE_TOOLS": "1"}), \
            patch.object(local_config, "DEFAULT_STATE_HOME", state_root), \
            arm_patch_context(arm):
        local_config.save_local_config(local_config.LocalProviderConfig(
            base_url=endpoint, model=model, api_protocol="openai-completions",
            native_tools_mode="auto", connection_revision="ghost-ab2-run",
            context=local_config.LocalContextBudget(262144, 32768, 32000)))
        state = AppContext(state_root)
        try:
            state._on_event_emitted = lambda row: events.append(dict(row))
            snapshot = state.providers.model_preferences.snapshot()
            sources = snapshot["sources"]
            sources["local"] = {"enabled": True, "models": [model]}
            state.providers.model_preferences.save(sources, base_revision=snapshot["revision"])
            state.ghost_inbox.set_learning_enabled(True)

            with patch.object(app_server.CodeyHTTPServer, "app_context", state):
                httpd = app_server.CodeyHTTPServer(("127.0.0.1", 0), app_server.Handler)
                thread = round1.threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    client = GateHTTPClient(httpd)
                    with rows_path.open("a", encoding="utf-8") as sink:
                        for task in TASKS:
                            for rep in range(reps):
                                session_id = f"ghostab2-{arm}-{task['id']}-r{rep}-{int(time.time()*1000)}"
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


# ---------------------------------------------------------------- report
def build_report() -> int:
    per_arm: dict[str, dict[str, dict[str, float]]] = {}
    for path in sorted(RUNS_DIR.glob("*.jsonl")):
        arm = path.stem
        table: dict[str, dict[str, float]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            cell = table.setdefault(row["task"], {"runs": 0, "passed": 0, "chars": 0.0})
            cell["runs"] += 1
            cell["chars"] += float(row.get("chars") or 0)
            if row.get("judge_passed"):
                cell["passed"] += 1
        per_arm[arm] = table
    lines = ["# Ghost directive round-2 report", ""]
    for arm, table in sorted(per_arm.items()):
        decisive_runs = sum(table[t]["runs"] for t in DECISIVE if t in table)
        decisive_pass = sum(table[t]["passed"] for t in DECISIVE if t in table)
        lines.append(f"## {arm} (decisive {decisive_pass}/{decisive_runs})")
        for task, cell in sorted(table.items()):
            avg = cell["chars"] / max(1, cell["runs"])
            lines.append(f"- {task}: {cell['passed']:.0f}/{cell['runs']:.0f} (chars_avg={avg:.1f})")
        lines.append("")
    text = "\n".join(lines)
    (HERE / "REPORT2.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed", "run", "report"])
    parser.add_argument("--arm", choices=["A2", "B2", "C2"])
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", default="koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M")
    parser.add_argument("--timeout-run", type=float, default=120.0)
    args = parser.parse_args()
    if args.command == "seed":
        arms = [args.arm] if args.arm else ["A2", "B2", "C2"]
        report = {}
        for arm in arms:
            report[arm] = seed_arm(arm, args.endpoint, args.model)
            print(json.dumps({"arm": arm, "nodes": report[arm].get("nodes"),
                              "sync_ok": report[arm].get("sync_ok")}, ensure_ascii=False))
            directive = report[arm].get("directive") or {}
            (HERE / f"directive_{arm}.txt").write_text(directive.get("text", ""), encoding="utf-8")
        (HERE / "directives2.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if args.command == "run":
        if not args.arm:
            raise SystemExit("run requires --arm")
        return run_arm(args.arm, args.reps, args.endpoint, args.model, args.timeout_run)
    return build_report()


if __name__ == "__main__":
    raise SystemExit(main())
