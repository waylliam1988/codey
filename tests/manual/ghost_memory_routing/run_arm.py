"""Run the live Ghost A/B battery for ONE arm through the real HTTP entry.

Mirrors tools/local_model_gate_shell.py isolation (own state home, real
AppContext + HTTP server, production task/admission path). Differences
between arms live only in the Local Context directive:
  A: production ranking (hebbian nodes + real affinity hints).
  B: same nodes, ranking hints synthesized from confirmation recency
     (+node weight), injected through the same render pipeline.
  C: no directive at all.

Usage:
  python run_arm.py --arm A --reps 3 --endpoint http://127.0.0.1:5001/v1 --model "<id>"
"""
from __future__ import annotations

import argparse
import dataclasses
import http.cookiejar
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, suppress
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO

HERE = DATA_ROOT
ARMS_ROOT = HERE / "arms"
RUNS_DIR = HERE / "runs"
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------- battery
TASKS = [
    {"id": "t0_control", "text": "What is 17 multiplied by 24? Reply with only the number.",
     "judge": "contains_408"},
    {"id": "t1_chinese", "text": "Explain the CAP theorem in plain words.",
     "judge": "cjk_dominant"},
    {"id": "t2_short", "text": "说明什么是数据库索引。",
     "judge": "short_answer"},
    {"id": "t3_list", "text": "List the benefits of unit tests.",
     "judge": "numbered_list"},
    {"id": "t4_pytest", "text": "这个项目里应该怎么跑测试？",
     "judge": "pytest_q"},
    {"id": "t5_answer_first", "text": "什么是死锁？请尽量直接回答。",
     "judge": "answer_first"},
]


def _cjk_ratio(text: str) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    alpha = len(re.findall(r"[A-Za-z]", text))
    if len(text) < 10:
        return 0.0
    return cjk / max(1.0, cjk + alpha)


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"[。！？.!?]+", text) if s.strip()])


def judge_answer(judge_name: str, text: str) -> dict:
    lowered = text.lower()
    detail = ""
    passed = False
    if judge_name == "contains_408":
        passed = "408" in text
    elif judge_name == "cjk_dominant":
        ratio = _cjk_ratio(text)
        detail = f"cjk_ratio={ratio:.2f}"
        passed = ratio > 0.3
    elif judge_name == "short_answer":
        count, length = _sentences(text), len(text)
        detail = f"sentences={count} chars={length}"
        passed = count <= 3 and length <= 300
    elif judge_name == "numbered_list":
        markers = len(re.findall(r"(?m)^\s*(?:\d+[\.\)]|[-*])\s", text))
        detail = f"list_lines={markers}"
        passed = markers >= 2
    elif judge_name == "pytest_q":
        has_q = "pytest -q" in lowered or "pytest  -q" in lowered
        has_py_m = "python -m pytest" in lowered
        detail = f"pytest_q={has_q} python_dash_m={has_py_m}"
        passed = has_q and not has_py_m
    elif judge_name == "answer_first":
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            detail = "empty"
        else:
            first = lines[0]
            rest = len(lines) - 1
            lead_ok = len(first) <= 72 and bool(re.search(r"[。.!?！？\)\)]\s*$|[a-zA-Z\u4e00-\u9fff]$", first))
            follow = sum(len(ln) for ln in lines[1:])
            detail = f"first_len={len(first)} follow_chars={follow} lead_ok={lead_ok}"
            passed = lead_ok and follow > 0 and rest >= 1
    return {"judge": judge_name, "passed": bool(passed), "detail": detail}


# ------------------------------------------------------- ranking variant B
@dataclasses.dataclass(frozen=True)
class SyntheticHint:
    kind: str
    target: str
    weight: float
    confidence: float


class BTimeAffinitySynth:
    """Same render pipeline, ranking hints from confirmation recency + weight."""

    def __init__(self, hebbian_store) -> None:
        self.hebbian_store = hebbian_store

    def query_directive_order_hints(self, nodes, project: str = "", session_id: str = ""):
        rows = sorted(self.hebbian_store.list_nodes(), key=lambda node: node.updated_at, reverse=True)
        max(1, len(rows))
        hints = []
        for rank, node in enumerate(rows):
            recency_strength = max(0.0, 1.0 - 0.14 * rank)
            strength = max(0.0, min(1.0, 0.8 * recency_strength + 0.2 * min(1.0, node.weight)))
            hints.append(SyntheticHint(kind="directive_order", target=node.id,
                                       weight=round(strength, 4), confidence=0.9))
        return tuple(hints)


# --------------------------------------------------------------- runner
class GateHTTPClient:
    def __init__(self, httpd):
        host, port = httpd.server_address
        self.origin = f"http://{host}:{port}"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.post("/api/operator_session", {"token": httpd.operator_auth.issue_bootstrap()})

    def request(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.origin + path, data=json.dumps(body).encode() if body is not None else None,
            headers={"Origin": self.origin, "Content-Type": "application/json"})
        try:
            response = self.opener.open(request, timeout=120)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def post(self, path: str, body: dict) -> dict:
        status, payload = self.request(path, body)
        if status not in {200, 202}:
            raise AssertionError(f"{path}: HTTP {status}: {payload}")
        return payload

    def state(self) -> dict:
        status, payload = self.request("/api/state")
        if status not in {200, 202}:
            raise AssertionError(f"state: HTTP {status}: {payload}")
        return payload


    def post_stop(self) -> dict:
        try:
            status, payload = self.request("/api/stop", {})
            return {"status": status, "payload": payload}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "payload": str(exc)}


def client_post_stop(client: GateHTTPClient) -> None:
    client.post_stop()


def final_answer_text(state, run_id: str, events: list[dict]) -> str:
    candidates = []
    for row in events:
        if row.get("run_id") not in {run_id, ""}:
            continue
        if row.get("type") in {"assistant_final", "assistant_message", "task_done"}:
            for key in ("text", "summary", "content"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value)
    if not candidates:
        with suppress(Exception):
            dict(state.durable.entries(run_id) if False else {})
    longest = max(candidates, key=lambda item: len(item)) if candidates else ""
    return longest


def run_arm(arm: str, reps: int, endpoint: str, model: str, timeout_run: float) -> int:
    RUNS_DIR.mkdir(exist_ok=True)
    state_root = ARMS_ROOT / arm
    rows_path = RUNS_DIR / f"{arm}.jsonl"
    mode = {"A": "affinity", "B": "btime", "C": "empty"}[arm]

    import codey.operations.ghost_context as ghost_context
    from codey.app import server as app_server
    from codey.app.context import AppContext
    from codey.ghost.directive import build_ghost_directive as real_build
    from codey.providers import local_config

    events: list[dict] = []
    env = {"NATIVE_TOOLS": "1"}
    with patch.dict(os.environ, env), patch.object(local_config, "DEFAULT_STATE_HOME", state_root):
        local_config.save_local_config(local_config.LocalProviderConfig(
            base_url=endpoint, model=model, api_protocol="openai-completions",
            native_tools_mode="auto", connection_revision="ghost-ab-run",
            context=local_config.LocalContextBudget(262144, 32768, 32000)))

        def patched_build(hebbian_store=None, **kwargs):
            return real_build(hebbian_store, **kwargs)

        if arm == "B":
            def b_build(store, **kwargs):
                synth = BTimeAffinitySynth(store)
                return real_build(store, **{**kwargs, "affinity_store": synth})
            patched = patch.object(ghost_context, "build_ghost_directive", b_build)
        else:
            patched = patch.object(ghost_context, "build_ghost_directive", patched_build)

        with patched:
            state = AppContext(state_root)
        stack = ExitStack()
        stack.callback(state.close)
        state._on_event_emitted = lambda row: events.append(dict(row))
        snapshot = state.providers.model_preferences.snapshot()
        sources = snapshot["sources"]
        sources["local"] = {"enabled": True, "models": [model]}
        state.providers.model_preferences.save(sources, base_revision=snapshot["revision"])
        state.ghost_inbox.set_learning_enabled(True)

        with patch.object(app_server.CodeyHTTPServer, "app_context", state):
            httpd = app_server.CodeyHTTPServer(("127.0.0.1", 0), app_server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                client = GateHTTPClient(httpd)
                with rows_path.open("a", encoding="utf-8") as sink:
                    for task in TASKS:
                        for rep in range(reps):
                            session_id = f"ghostab-{arm}-{task['id']}-r{rep}-{int(time.time()*1000)}"
                            submitted = client.post("/api/run", {
                                "session_id": session_id, "input": {"kind": "start_task", "text": task["text"]},
                                "provider": "local", "model_selection": {"model": model, "base_url": endpoint},
                                "intent": "chat", "max_turns": 4})
                            run_id = submitted["run_id"]
                            terminal, deadline = {}, time.monotonic() + timeout_run
                            events.clear()
                            while time.monotonic() < deadline:
                                snapshot_state = client.state()
                                event = snapshot_state.get("last_terminal_event") or {}
                                if event.get("run_id") == run_id and not snapshot_state.get("busy"):
                                    terminal = event
                                    break
                                time.sleep(0.05)
                            stop_reason = terminal.get("stop_reason") or {}
                            if isinstance(stop_reason, dict):
                                stop_reason = stop_reason.get("value")
                            text = terminal.get("summary") or ""
                            if (stop_reason != "done"):
                                text_extra = final_answer_text(state, run_id, events)
                                text = text or text_extra
                            judgement = judge_answer(task["judge"], text) if text else {
                                "judge": task["judge"], "passed": False, "detail": "no_text"}
                            row = {
                                "arm": arm, "mode": mode, "task": task["id"], "rep": rep,
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
                stack.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True, choices=["A", "B", "C"])
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout-run", type=float, default=120.0)
    args = parser.parse_args()
    for stale in (RUNS_DIR / f"{args.arm}.jsonl",):
        if stale.exists():
            stale.unlink()
    return run_arm(args.arm, args.reps, args.endpoint, args.model, args.timeout_run)


if __name__ == "__main__":
    raise SystemExit(main())
