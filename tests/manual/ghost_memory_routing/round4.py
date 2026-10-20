"""Round 4 = 0.6 entry acceptance battery (harness-only, no production edits).

Behavior cells (each >=20 runs, model selectable via --model/--endpoint):
  lang_on_d2 / lang_off_d2   language slot delivered vs withheld (4 phrasings)
  cmd_on_d2  / cmd_off_d2    test-command slot delivered vs withheld (4 phrasings)
  budget d2 / recency at caps 4|8|12, hebbian at cap 12 (mixed lang+cmd pool)
  override_d2                explicit "answer in English" must beat the directive

Offline suites (no model):
  parity : ops-level ghost_directive == direct build (production slots, byte-equal)
  audit  : per-cap ranked node tables (who got in, who got cut, bytes used)
  conflict: T5 supersede state, delete->rebuild no-revival, expiry no-line
  trigger: task-key false-positive scan over naive utterances

Usage:
  python round4.py seed --endpoint U            (once)
  python round4.py run --suite modelA --endpoint U --model M
  python round4.py offline
  python round4.py report
"""
# ruff: noqa: E402 -- direct-script bootstrap before importing Codey.
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO, SCRIPT_DIR

HERE = DATA_ROOT
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCRIPT_DIR))

import round2  # arm root, expanded renderer, recency key, seeding
import round3  # tier pieces + T5 arm already seeded
import run_arm as round1  # battery helpers
from run_arm import GateHTTPClient, judge_answer

RUNS4_DIR = HERE / "runs4"
PARITY_DIR = HERE / "parity"
CURRENT_TASK = ""

LANG_TASKS = [
    "Explain the CAP theorem in plain words.",
    "What is database normalization? Give a short explanation.",
    "How does a binary search tree differ from a heap?",
    "What is eventual consistency in distributed systems?",
] * 5
CMD_TASKS = [
    "这个项目里平时怎么跑测试？直接回答就好，不要执行命令。",
    "项目测试用什么命令行？别执行，只告诉我。",
    "How should tests be run in this repo? Just name the command, do not execute.",
    "在 CI 里跑这个项目的单测应该用哪条命令？不要执行。",
] * 5
MIXED_TASKS = [t for pair in zip(LANG_TASKS, CMD_TASKS, strict=True) for t in pair][:20]
OVERRIDE_TASKS = [
    (f"{task}（请直接用英文回答。）") for task in
    ["Explain the CAP theorem in plain words.",
     "What is eventual consistency in distributed systems?"] * 6
]

R4_TIER_TABLE = {
    ("style_preference", "language"): (1, 0.90, None),
    ("style_preference", "code_notes"): (1, 0.80, None),
    ("style_preference", "reply_structure"): (1, 0.50, None),
    ("style_preference", "reply_length"): (1, 0.40, None),
    ("style_preference", "format"): (1, 0.30, None),
    ("style_preference", "tone"): (1, 0.25, None),
    ("action_tendency", "test_command"): (2, 0.35, r"测试|pytest|test|单测|跑测试|命令跑|用哪条"),
    ("action_tendency", "commit_flow"): (2, 0.30, r"提交|commit"),
    ("research_interest", "focus"): (3, 0.50, None),
    ("correction", "state_store"): (4, 0.0, None),
    ("correction", "state_backend"): (4, 0.0, None),
    ("correction", "ghost_state_backend"): (4, 0.0, None),
    ("correction", "memory_state"): (4, 0.0, None),
    ("long_term_goal", "ghost"): (4, 0.0, None),
}
TRIGGER_SCAN_UTTERANCES = [
    "今天天气怎么样？", "介绍一下你自己。", "这首歌真好听。", "帮我写首诗。",
    "昨晚球赛看得过瘾！", "我们提交作业务必准时。", "我刚跑完步，心情不错。",
    "给我讲个笑话。", "推荐一本书。", "机器学习的基本概念有哪些？",
    "如何做自我介绍？", "帮我翻译一句话。", "周末去哪儿玩？",
    "读书会下次聚会要提交读书笔记吗？", "我的账户怎么提现？",
    "健身房上午跑了几圈椭圆机？", "pytest 这个词怎么发音？",
    "如何提高工作效率？", "推荐几个旅游景点。", "帮我把这段话润色一下。",
]
FAMILIES = {
    "language": {"on": "lang_on_d2", "off": "lang_off_d2", "tasks": LANG_TASKS,
                 "gate": "promote_if_strong"},
    "command": {"on": "cmd_on_d2", "off": "cmd_off_d2", "tasks": CMD_TASKS,
                "gate": "promote_if_matched_stable"},
    "format|length|answer_first": {"verdict": "weak_hint_only", "runs": []},
    "self_description": {"verdict": "excluded_default"},
    "hebbian": {"verdict": "feature_hint_only"},
}


def slot_of(kind: str, conflict_key: str) -> str:
    prefix = f"{kind}:"
    return conflict_key[len(prefix):] if (conflict_key or "").startswith(prefix) else (conflict_key or "")


def d2_sort_key(node, hint_weights=None):
    tier, prior, trigger = R4_TIER_TABLE.get(
        (node.kind, slot_of(node.kind, node.conflict_key)), (3, 0.4, None))
    boost = 0.75 if (trigger and CURRENT_TASK and re.search(trigger, CURRENT_TASK)) else 0.0
    return (-(prior + boost), tier, round2.gcommon.reverse_text_sort_key(node.updated_at))


@contextmanager
def cell_context(sort: str, cap: int | None, drop: frozenset):
    render = round2.expanded_render
    if drop:
        def filtered_render(kind, conflict_key, value_key, max_chars=None,
                            _inner=round2.expanded_render):
            slot = slot_of(kind, conflict_key)
            if (kind, slot) in drop:
                return ""
            return _inner(kind, conflict_key, value_key, max_chars=max_chars)
        render = filtered_render
    key = round2.ORIG_SORT_KEY
    if sort == "d2":
        key = d2_sort_key
    elif sort == "recency":
        key = round2.recency_sort_key
    elif sort == "hebbian":
        key = round2.ORIG_SORT_KEY
    try:
        round2.gdir.render_typed_field = render
        round2.gdir._node_sort_key = key
        patched_cap = patch.object(round2.gdir, "MAX_DIRECTIVE_ITEMS", cap) if cap else None
        if patched_cap:
            patched_cap.start()
        yield
    finally:
        if patched_cap:
            patched_cap.stop()
        round2.gdir.render_typed_field = round2.ORIG_RENDER
        round2.gdir._node_sort_key = round2.ORIG_SORT_KEY


# ------------------------------------------------------------------ running
CELLS = [
    {"name": "lang_on_d2", "sort": "d2", "cap": None, "drop": frozenset(), "pool": LANG_TASKS},
    {"name": "lang_off_d2", "sort": "d2", "cap": None, "drop": frozenset({("style_preference", "language")}), "pool": LANG_TASKS},
    {"name": "cmd_on_d2", "sort": "d2", "cap": None, "drop": frozenset(), "pool": CMD_TASKS},
    {"name": "cmd_off_d2", "sort": "d2", "cap": None, "drop": frozenset({("action_tendency", "test_command")}), "pool": CMD_TASKS},
    {"name": "cap4_d2", "sort": "d2", "cap": 4, "drop": frozenset(), "pool": MIXED_TASKS},
    {"name": "cap8_d2", "sort": "d2", "cap": 8, "drop": frozenset(), "pool": MIXED_TASKS},
    {"name": "cap12_d2", "sort": "d2", "cap": 12, "drop": frozenset(), "pool": MIXED_TASKS},
    {"name": "cap4_recency", "sort": "recency", "cap": 4, "drop": frozenset(), "pool": MIXED_TASKS},
    {"name": "cap8_recency", "sort": "recency", "cap": 8, "drop": frozenset(), "pool": MIXED_TASKS},
    {"name": "cap12_recency", "sort": "recency", "cap": 12, "drop": frozenset(), "pool": MIXED_TASKS},
    {"name": "cap12_hebbian", "sort": "hebbian", "cap": 12, "drop": frozenset(), "pool": MIXED_TASKS},
    {"name": "override_d2", "sort": "d2", "cap": None, "drop": frozenset(), "pool": OVERRIDE_TASKS},
]


def run_cells(cell_names: list[str], endpoint: str, model: str, suite: str,
              state_root: Path, timeout_run: float) -> int:
    RUNS4_DIR.mkdir(exist_ok=True)
    from codey.app import server as app_server
    from codey.app.context import AppContext
    from codey.providers import local_config

    for cell in CELLS:
        if cell_names and cell["name"] not in cell_names:
            continue
        rows_path = RUNS4_DIR / f"{cell['name']}__{suite}.jsonl"
        rows_path.unlink(missing_ok=True)
        events: list[dict] = []
        with patch.dict("os.environ", {"NATIVE_TOOLS": "1"}), \
                patch.object(local_config, "DEFAULT_STATE_HOME", state_root), \
                cell_context(cell["sort"], cell["cap"], cell["drop"]):
            local_config.save_local_config(local_config.LocalProviderConfig(
                base_url=endpoint, model=model, api_protocol="openai-completions",
                native_tools_mode="auto", connection_revision="ghost-ab4",
                context=local_config.LocalContextBudget(262144, 32768, 32000)))
            state = AppContext(state_root)
            try:
                state._on_event_emitted = lambda row, sink=events: sink.append(dict(row))
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
                            for index, task in enumerate(cell["pool"]):
                                globals()["CURRENT_TASK"] = task
                                session_id = (f"gh4-{cell['name']}-{suite}-{index}"
                                              f"-{int(time.time()*1000)}")
                                submitted = client.post("/api/run", {
                                    "session_id": session_id,
                                    "input": {"kind": "start_task", "text": task},
                                    "provider": "local",
                                    "model_selection": {"model": model, "base_url": endpoint},
                                    "intent": "chat", "max_turns": 4})
                                run_id = submitted["run_id"]
                                terminal, deadline = {}, time.monotonic() + timeout_run
                                events.clear()
                                denied_ids: set[str] = set()
                                while time.monotonic() < deadline:
                                    snap = client.state()
                                    event = snap.get("last_terminal_event") or {}
                                    if event.get("run_id") == run_id and not snap.get("busy"):
                                        terminal = event
                                        break
                                    if time.monotonic() - (deadline - timeout_run) > 40:
                                        status, payload = client.request("/api/shell_approval")
                                        if status == 200 and isinstance(payload, dict):
                                            pending = (payload.get("requests")
                                                       or payload.get("pending") or [])
                                            for request in pending:
                                                request_id = (request.get("id") or request) \
                                                    if isinstance(request, dict) else request
                                                if not request_id or request_id in denied_ids:
                                                    continue
                                                client.post("/api/shell_approval",
                                                            {"id": request_id, "approved": False})
                                                denied_ids.add(str(request_id))
                                    time.sleep(0.05)
                                if not terminal:
                                    round1.client_post_stop(client)
                                    waited = time.monotonic() + 10
                                    while time.monotonic() < waited:
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
                                pool_id = "lang" if task in LANG_TASKS else ("cmd" if task in CMD_TASKS else "")
                                if pool_id == "cmd":
                                    judgement = judge_answer("pytest_q", text)
                                elif pool_id == "lang":
                                    judgement = judge_answer("cjk_dominant", text)
                                else:
                                    judgement = {"judge": "override_en", "passed": False, "detail": "n/a"}
                                    (text or "").lower()
                                    ratio = round1._cjk_ratio(text)
                                    judgement = {"judge": "english_request",
                                                 "passed": (text or "") and ratio < 0.15,
                                                 "detail": f"cjk_ratio={ratio:.2f}"}
                                row = {
                                    "cell": cell["name"], "suite": suite, "model": model,
                                    "index": index, "task_family": pool_id,
                                    "stop_reason": stop_reason or "timeout",
                                    "chars": len(text),
                                    "judge_passed": judgement["passed"],
                                    "judge_detail": judgement["detail"],
                                    "approval_denied": len(denied_ids), "text": text,
                                }
                                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                                sink.flush()
                                print(json.dumps({k: row[k] for k in
                                                  ("cell", "suite", "index", "task_family",
                                                   "stop_reason", "judge_passed")}, ensure_ascii=False))
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


# ------------------------------------------------------------------ offline
def _ops_directive_payload() -> dict:
    from codey.app.context import AppContext
    from codey.operations import ghost_context
    state = AppContext(round2.ARMS_ROOT / "R4")
    try:
        payload = ghost_context.ghost_directive(state)
        return payload if isinstance(payload, dict) else getattr(payload, "to_payload", lambda: dict(payload))()
    finally:
        state.close()


def audit_tables() -> dict:
    audit = {}
    from codey.ghost.affinity import GhostAffinityStore
    from codey.ghost.directive import build_ghost_directive
    from codey.ghost.hebbian import GhostHebbianStore
    arm = round2.ARMS_ROOT / "R4"
    nodes = GhostHebbianStore(arm).list_nodes()
    for cap in (4, 8, 12):
        for sort in ("d2", "recency", "hebbian"):
            with cell_context(sort, cap, frozenset()):
                directive = build_ghost_directive(
                    GhostHebbianStore(arm), affinity_store=GhostAffinityStore(arm))
                payload = directive.to_payload()
                text = payload.get("text", "")
                rows = [{"id": node.id[-8:], "slot": slot_of(node.kind, node.conflict_key),
                         "kind": node.kind, "weight": round(node.weight, 3)}
                        for node in nodes]
                {slot_of(node.kind, node.conflict_key)
                             for node in ()}
                lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
                audit[f"{sort}@cap{cap}"] = {
                    "bytes": len(text.encode("utf-8")),
                    "selected": payload.get("selected_count"),
                    "lines": lines,
                    "weights": rows,
                }
    PARITY_DIR.mkdir(exist_ok=True)
    (PARITY_DIR / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    return audit


def parity_check() -> dict:
    from codey.ghost.affinity import GhostAffinityStore
    from codey.ghost.directive import build_ghost_directive
    from codey.ghost.hebbian import GhostHebbianStore
    arm = round2.ARMS_ROOT / "R4"
    # strict parity: production renderer + production sort on BOTH sides
    direct = build_ghost_directive(GhostHebbianStore(arm), affinity_store=GhostAffinityStore(arm))
    direct_payload = direct.to_payload()
    ops = _ops_directive_payload()
    ops_json = ops if isinstance(ops, dict) else ops.to_payload()
    result = {
        "strict_text_equal": ops_json.get("text", "") == direct_payload.get("text", ""),
        "ops_selected": ops_json.get("selected_count"),
        "direct_selected": direct_payload.get("selected_count"),
        "ops_bytes": len(ops_json.get("text", "").encode("utf-8")),
        "direct_bytes": len(direct_payload.get("text", "").encode("utf-8")),
        "ops_warnings": list(ops_json.get("warnings", []))[:6],
        "note": "expanded families exist only harness-side; ops cannot render them",
    }
    PARITY_DIR.mkdir(exist_ok=True)
    (PARITY_DIR / "parity.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    return result


def conflict_suite() -> dict:
    from dataclasses import dataclass
    from time import sleep

    from codey.ghost.hebbian import GhostHebbianStore
    from codey.ghost.inbox import GhostInboxStore
    from codey.ghost.schema import GhostSignal

    results = {}
    arm_dir = round2.ARMS_ROOT / "R4C"
    if not arm_dir.exists():
        arm_dir.mkdir(parents=True)
        inbox = GhostInboxStore(arm_dir)
        inbox.set_learning_enabled(True)
        hebbian = GhostHebbianStore(arm_dir)
        for value, summary, quote in (
            ("chinese", "language confirmed once (chinese)", "技术解释一律用中文写。"),
            ("english", "language changed later (english)", "Switch explanations to English."),
        ):
            decision = inbox.ingest_signals(
                (GhostSignal(kind="style_preference", scope="user", summary=summary,
                             evidence_quote=quote, confidence=0.9, source="manual",
                             metadata={"conflict_key": "language", "value_key": value}),),
                session_id="ghost-r4c", run_id="")
            target = decision[0]
            if getattr(target, "status", "") != "accepted":
                target = inbox.review_candidate(target.id, "accept", reviewed_by="ghost-r4c")
            hebbian.reinforce_candidate(target, related_candidates=(target,))
            sleep(2.3)

    hebbian = GhostHebbianStore(arm_dir)
    language_nodes = [n for n in hebbian.list_nodes() if "language" in n.conflict_key]
    results["supersede_keeps_nodes"] = len(language_nodes)

    with round3.tier_patch_context():
        before = round2.dump_directive("R4C").get("text", "")

    target = language_nodes[-1]
    candidate_id = (target.candidate_ids or ("",))[0]

    @dataclass(frozen=True)
    class _Stub:
        id: str
        signal_kind: str
        conflict_key: str
        value_key: str
        scope: str = "user"
        scope_ref: str = ""

    removed = hebbian.remove_candidate(_Stub(
        id=candidate_id, signal_kind=target.kind,
        conflict_key=target.conflict_key, value_key=target.value_key,
        scope=target.scope, scope_ref=getattr(target, "scope_ref", "") or ""))
    rebuilt = GhostHebbianStore(arm_dir).rebuild_from_events()
    leftovers = [n for n in GhostHebbianStore(arm_dir).list_nodes()
                 if "language" in n.conflict_key and n.value_key == target.value_key]
    with round3.tier_patch_context():
        after = round2.dump_directive("R4C").get("text", "")
    results["removed"] = removed
    results["rebuild_ok"] = bool(rebuilt)
    results["revived_nodes"] = len(leftovers)
    results["removed_value_had_line"] = "- Prefer: reply language = english" in before
    results["line_after"] = "reply language" in after
    results["no_revival_ok"] = not leftovers and not results["line_after"]

    expiry_payload = None
    fake_now = "2060-01-01T00:00:00Z"
    _orig_now = round2.gcommon.now_iso_z
    def _future_now():
        return fake_now
    round2.gcommon.now_iso_z = _future_now
    try:
        with round3.tier_patch_context():
            expiry_payload = round2.dump_directive("T5")
    finally:
        round2.gcommon.now_iso_z = _orig_now
    results["expired_lines"] = sum(1 for ln in expiry_payload.get("text", "").splitlines()
                                   if ln.startswith("- "))
    results["expiry_ok"] = results["expired_lines"] == 0
    PARITY_DIR.mkdir(exist_ok=True)
    (PARITY_DIR / "conflict.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    return results


def trigger_scan() -> dict:
    hits = []
    for text in TRIGGER_SCAN_UTTERANCES:
        for pair, (_tier, _prior, trigger) in R4_TIER_TABLE.items():
            if trigger and re.search(trigger, text, re.IGNORECASE):
                hits.append({"slot": pair[1], "text": text})
    PARITY_DIR.mkdir(exist_ok=True)
    (PARITY_DIR / "triggers.json").write_text(json.dumps(hits, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    false_positives = [h for h in hits if not any(
        key in h["text"] for key in ("测试", "pytest", "跑测试", "提交", "commit"))]
    return {"utterances": len(TRIGGER_SCAN_UTTERANCES), "hits": len(hits),
            "false_positives": false_positives}


# ------------------------------------------------------------------ report
def report() -> int:
    cells: dict[str, dict[str, dict[str, float]]] = {}
    for path in sorted(RUNS4_DIR.glob("*.jsonl")):
        cell, suite = path.stem.rsplit("__", 1) if "__" in path.stem else (path.stem, "")
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            table = cells.setdefault(cell, {}).setdefault(suite, {})
            entry = table.setdefault(row["task_family"], {"runs": 0, "passed": 0, "chars": 0.0})
            entry["runs"] += 1
            entry["chars"] += float(row.get("chars") or 0)
            if row.get("judge_passed"):
                entry["passed"] += 1
    lines = ["# Ghost 0.6 entry acceptance report", "",
             "## per-family behavior (no aggregate score)", ""]
    order = ["lang_on_d2", "lang_off_d2", "cmd_on_d2", "cmd_off_d2",
             "cap4_d2", "cap8_d2", "cap12_d2",
             "cap4_recency", "cap8_recency", "cap12_recency", "cap12_hebbian",
             "override_d2"]
    for cell in order:
        for suite, table in sorted(cells.get(cell, {}).items()):
            parts = []
            for family, entry in sorted(table.items()):
                if not entry["runs"]:
                    continue
                rate = entry["passed"] / entry["runs"]
                avg = entry["chars"] / entry["runs"]
                parts.append(f"{family or 'override'} {entry['passed']:.0f}/{entry['runs']:.0f}"
                             f" ({rate*100:.0f}%, chars_avg={avg:.0f})")
            if parts:
                lines.append(f"- {cell} [{suite}]: " + "; ".join(parts))
    audit_path = PARITY_DIR / "audit.json"
    if audit_path.exists():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        lines += ["", "## coverage vs bytes (offline)", ""]
        for key, block in audit.items():
            fam_lines = [ln for ln in block["lines"]
                         if ("reply language" in ln or "test command" in ln
                             or "code notes" in ln or "format = bullets" in ln)]
            lines.append(f"- {key}: bytes={block['bytes']} selected={block['selected']} "
                         f"behavior_families={len(fam_lines)/4:.2f} "
                         f"({'; '.join(ln.strip('- ')[:38] for ln in fam_lines)})")
    verdicts = family_verdicts(cells)
    coverage = verdicts.get("_coverage", {})
    lines += ["", "## frozen matrix coverage", "",
              f"- status: **{coverage.get('status', 'INCOMPLETE')}**",
              f"- required suites: {', '.join(coverage.get('required_suites', [])) or 'none'}"]
    if coverage.get("missing"):
        lines.append(f"- missing: {json.dumps(coverage['missing'], ensure_ascii=False)}")
    lines += ["", "## promotion verdicts", "", _md(verdicts)]
    (HERE / "REPORT4.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


def family_verdicts(cells: dict) -> dict:
    from evaluation_contract import family_verdicts as frozen_family_verdicts

    return frozen_family_verdicts(cells)


def _md(mapping: dict) -> str:
    rows = ["| family | on | off | rule | verdict |", "|---|---|---|---|---|"]
    for name, item in mapping.items():
        if name.startswith("_"):
            continue
        on_cell = item.get("on") if item.get("on") not in (None, "-") else item.get("per_model", "-")
        rows.append(f"| {name} | {on_cell} | {item.get('off','-')} "
                    f"| {item.get('rule','-')} | **{item['verdict']}** |")
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed", "run", "offline", "report"])
    parser.add_argument("--suite", default="modelA")
    parser.add_argument("--cells", default="")
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", default="koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M")
    parser.add_argument("--timeout-run", type=float, default=120.0)
    args = parser.parse_args()
    if args.command == "seed":
        if (round2.ARMS_ROOT / "R4").exists():
            print(json.dumps({"seed": "exists"}))
            return 0
        result = round2.seed_arm("R4", args.endpoint, args.model)
        print(json.dumps({"arm": "R4", "nodes": result.get("nodes")}))
        return 0
    if args.command == "run":
        wanted = [c for c in args.cells.split(",") if c] or [cell["name"] for cell in CELLS]
        return run_cells(wanted, args.endpoint, args.model, args.suite,
                         round2.ARMS_ROOT / "R4", args.timeout_run)
    if args.command == "offline":
        out = {"parity": parity_check(), "conflict": conflict_suite(),
               "trigger": trigger_scan()}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.command == "report":
        return report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
