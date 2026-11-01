"""Compare real research entries on frozen evidence; never grade with the tested model.

Diagnostic Codey runs reuse the explicitly labelled fixed-plan/read fixture. The
synthesis runs use the ordinary entry with model-generated planning. OpenScience
uses the unmodified release CLI. A transparent local proxy records and bounds
inference, not the agent's decisions or tool protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from tests.manual.research_evidence_local import DEFAULT_CLOCK_FILE, QUESTIONS, fixture_clock, fixture_windows
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment

UPSTREAM = "http://127.0.0.1:5001"
SEEDS = (74, 75, 76)
DIAGNOSTIC = ("writer_limit", "readonly_conditions", "checkpoint_stop", "checkpoint_resume", "coexistence_only")
SYNTHESIS = {
    "wal_synthesis": {
        "question": "基于所给 SQLite 官方材料，用中文逐项研究：\n"
            "1. WAL 的读写并发和单写者限制是否矛盾？分别解释机制。\n"
            "2. WAL 引入版本、只读打开支持版本及完整替代条件是什么？保留或的关系。\n"
            "3. checkpoint 与读者并行时何时停止、为什么、进度保存在哪里、以后如何继续？\n"
            "4. writer 回绕复用 WAL 的条件是什么？这些条件是否也是 checkpoint 转移数据的必要条件？区分两个动作。\n"
            "5. 这些材料能否证明本机 WAL 比回滚日志更快，或证明整份官方页面从未做过本机测量？说明证据边界。",
        "required": ["读者与写者互不阻塞", "单写者：一个 WAL 文件", "WAL 引入 3.7.0",
            "只读从 3.22.0 支持", "已有可读 shm/wal", "有权创建 shm/wal", "immutable",
            "三个只读替代条件是 OR", "checkpoint 越过任一当前 reader end mark 停止",
            "防止覆盖 reader 使用的数据库页", "进度在 wal-index", "下次调用从记录进度继续",
            "writer 回绕：WAL 已全部 checkpoint 且没有 reader 仍在使用 WAL",
            "回绕和 checkpoint 条件不同，checkpoint 可以与读者并行并部分转移",
            "不能从一般优点证明本机性能", "不能从选段缺少测量推断整页没有测量"],
        "relations": ["读写并发与单写者并存", "只读 OR 的适用版本", "停止原因与继续机制",
            "writer 回绕与 checkpoint 转移的不同前提", "片段到本机性能/全文断言的外推边界"],
    },
    "asyncio_synthesis": {
        "question": "基于所给 Python 3.12 官方材料，用中文逐项研究 asyncio.gather：\n"
            "1. 默认 return_exceptions=False 时，一个子任务抛异常会怎样？其余任务是否自动取消？\n"
            "2. return_exceptions=True 改变什么？成功结果的排列按输入顺序还是完成顺序？\n"
            "3. 取消 gather 本身与取消其中一个 Task/Future 有什么不同？保留未完成任务、CancelledError 及 return_exceptions 的边界。\n"
            "4. 捕获 gather 传播的异常之后再调用 gather.cancel()，是否必然取消其余任务？为什么？\n"
            "5. TaskGroup 对普通子任务异常的取消保证与 gather 有什么区别？这些材料是否足以证明 TaskGroup 总是更快？",
        "required": ["默认首个异常立即传播给 await gather 的任务", "其余 awaitables 不自动取消并继续运行",
            "True 把异常作为结果汇总", "成功结果按输入顺序", "取消 gather 本身会取消尚未完成的 awaitables",
            "取消 gather 的传播不受 return_exceptions 影响", "单个子 Task/Future 取消按 CancelledError 处理",
            "单个子取消不使 gather 本身取消", "不因此连带取消其他子任务",
            "gather 传播异常后可能已 done，此时 cancel 不取消剩余任务",
            "TaskGroup 普通任务异常取消剩余任务而 gather 不会", "取消保证不证明 TaskGroup 总是更快"],
        "relations": ["默认异常传播与剩余任务的不同命运", "异常作为值与结果顺序",
            "父 gather 取消与单个子任务取消", "done 状态约束后续 cancel 的效果",
            "TaskGroup 安全保证与性能结论的区别"],
    },
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def experiment_limits(root):
    """Keep exploratory budget changes explicit and separate from the main series."""
    limits = {"turns": 12, "requests": 12, "process_seconds": 360, "agent_seconds": 300}
    path = root / "limits.json"
    if path.exists():
        overrides = read_json(path)
        if (not isinstance(overrides, dict) or set(overrides) - limits.keys()
                or any(type(value) is not int or value <= 0 for value in overrides.values())):
            raise ValueError("limits.json must contain only positive integer experiment limits")
        limits.update(overrides)
    return limits


def sampling(payload, seed):
    """No tool/schema/message translation: keep each product's actual protocol."""
    result = {**payload, "seed": seed, "temperature": 0.3, "top_p": 0.9}
    result["max_tokens"] = min(payload.get("max_tokens") or 4096, 4096)
    result["chat_template_kwargs"] = {**payload.get("chat_template_kwargs", {}), "enable_thinking": False}
    return result


def comparison_config(config):
    """Configure the real admission path, rather than altering server metadata."""
    from codey.providers.local_config import LocalContextBudget

    return replace(config, context=LocalContextBudget(32768, 4096, 12000, source="configuration"))


def backend_ready(upstream=UPSTREAM):
    """Do not turn a stalled backend into another framework's failed trial."""
    with urlopen(upstream + "/api/extra/perf", timeout=5) as response:
        perf = json.load(response)
    if perf.get("idle") != 1 or perf.get("queue") != 0:
        raise RuntimeError(f"Evaluation stopped before inference: backend not idle: {perf}")
    return perf


class InferenceProxy:
    def __init__(self, wire, seed, *, upstream=UPSTREAM, request_limit=12, genkey=None, identity_root=None):
        self.wire, self.seed, self.upstream, self.request_limit = Path(wire), seed, upstream, request_limit
        self.genkey = genkey
        self.requests, self.tokens = 0, 0
        self.lock, self.file_lock = threading.Lock(), threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                inference = self.path == "/v1/chat/completions"
                with owner.lock:
                    if inference:
                        original = json.loads(body)
                        if identity_root is not None:
                            try:
                                verify_experiment(identity_root)
                            except RuntimeError as error:
                                owner.record({"event": "source_identity_rejected", "error": str(error)})
                                response = json.dumps({"error": {"message": str(error), "code": "evaluation_source_changed"}}).encode()
                                self.send_response(409)
                                self.send_header("Content-Type", "application/json")
                                self.send_header("Content-Length", str(len(response)))
                                self.end_headers()
                                self.wfile.write(response)
                                return
                        if owner.requests >= owner.request_limit or owner.tokens >= 180_000:
                            response = json.dumps({"error": {"message": "Frozen evaluation inference budget exhausted",
                                "type": "evaluation_budget", "code": "evaluation_budget"}}).encode()
                            owner.record({"event": "budget_rejected", "requested_payload": original})
                            self.send_response(400)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(response)))
                            self.end_headers()
                            self.wfile.write(response)
                            return
                        payload = sampling(original, owner.seed)
                        if owner.genkey:
                            payload["genkey"] = owner.genkey
                        owner.requests += 1
                        owner.record({"event": "admitted_payload", "index": owner.requests,
                                      "original_payload": original, "payload": payload})
                        body = json.dumps(payload, ensure_ascii=False).encode()
                    started = time.monotonic()
                    headers = {"Content-Type": "application/json"} if body else {}
                    request = Request(owner.upstream + self.path, data=body if self.command == "POST" else None,
                                      headers=headers, method=self.command)
                    captured = bytearray()
                    try:
                        try:
                            response = urlopen(request, timeout=150)
                        except HTTPError as error:
                            response = error
                        self.send_response(response.status)
                        for key, value in response.headers.items():
                            if key.lower() not in {"transfer-encoding", "connection", "content-length", "server", "date"}:
                                self.send_header(key, value)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        with response:
                            while chunk := response.read1(65536):
                                captured.extend(chunk)
                                self.wfile.write(chunk)
                                self.wfile.flush()
                        self.close_connection = True
                    except (OSError, TimeoutError) as error:
                        owner.record({"event": "proxy_failure", "error": repr(error)})
                        self.close_connection = True
                    finally:
                        if inference:
                            perf = None
                            try:
                                with urlopen(owner.upstream + "/api/extra/perf", timeout=5) as reply:
                                    perf = json.load(reply)
                            except OSError:
                                pass
                            if perf and perf.get("last_seed") == owner.seed:
                                owner.tokens += perf.get("last_input_count", 0)
                            owner.record({"event": "generation_response", "index": owner.requests,
                                "response_text": captured.decode("utf-8", errors="replace"), "backend_perf": perf,
                                "elapsed_seconds": round(time.monotonic() - started, 3)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True

    def record(self, row):
        self.wire.parent.mkdir(parents=True, exist_ok=True)
        with self.file_lock, self.wire.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def documents(root, case):
    if case in DIAGNOSTIC:
        full = next(doc for doc in read_json(root / "sqlite-sources.json") if doc["url"] == "https://www.sqlite.org/wal.html")
        return [{**full, "text": full["text"][row["offset"]:row["offset"] + row["limit"]],
                 "selection": row} for row in fixture_windows(full["text"], case, "given", 0)]
    return read_json(root / ("wal-material.json" if case == "wal_synthesis" else "asyncio-material.json"))


def material_text(root, case):
    return "\n\n".join(f"[材料 {i}] {doc['title']}\n来源：{doc['url']}\n{doc['text']}"
                           for i, doc in enumerate(documents(root, case), 1))


def task_text(root, case):
    question = QUESTIONS[case] if case in DIAGNOSTIC else SYNTHESIS[case]["question"]
    channel = root / "material-channel.json"
    if channel.exists() and read_json(channel)["mode"] == "read":
        urls = "\n".join(doc["url"] for doc in documents(root, case))
        return (question + "\n用中文作答。仅根据本轮固定材料，不能确定的部分明确保留缺口，不使用材料外信息。"
                "需要先使用本框架可用的材料读取工具读取原文；"
                "官方 URL 在测评的来源读取接口中映射为固定快照，本地 evidence.md 保存同一份快照。"
                "使用可用的来源读取接口或本地文件读取接口即可，不要求重复读取。\n来源：\n" + urls)
    material = material_text(root, case)
    if case in DIAGNOSTIC:
        return (question + "用中文作答。仅根据提供的已打开证据形成局部结论，不能确定的部分如实说明。"
                "\n\n以下是提供的已打开证据（同样内容保存在本地 evidence.md）：\n" + material)
    return (question + "\n用中文作答，仅根据所给材料形成有依据的回答；不能确定的部分明确保留缺口。"
            "不得将选段未提及扩大成整页没有说明，也不得把邻近事实作为所问原因。"
            "说明依据，区分事实、关系及未测量的结论。无需用户补充材料。\n"
            "本次为封闭材料比较，不使用材料外信息。下面的官方来源内容是固定快照；"
            "若需要重读，可以使用可用的材料读取工具，本地 evidence.md 含同样材料。\n\n" + material)


def openscience_environment(root, directory, base_url, model):
    config = {"enabled_providers": ["localbench"], "model": "localbench/" + model,
        "small_model": "localbench/" + model, "autoupdate": False,
        "provider": {"localbench": {"name": "Local evaluation", "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": base_url + "/v1", "apiKey": "local", "timeout": 150000},
            "models": {model: {"name": "Gemma4 12B", "tool_call": True, "reasoning": False,
                "temperature": True, "limit": {"context": 32768, "output": 4096}}}}},
        "agent": {"research": {"temperature": 0.3, "steps": experiment_limits(root)["turns"]}},
        "permission": {"task": "deny", "question": "deny", "external_directory": "deny",
            "bash": "deny", "python": "deny", "compute_job": "deny", "webfetch": "deny",
            "research_search": "deny", "literature": "deny", "science_search": "deny", "science_fetch": "deny"},
        "billing": {"llm": "byok"}}
    write_json(directory / "config.json", config)
    return {**os.environ, "OPENSCIENCE_CONFIG": str(directory / "config.json"),
        "OPENSCIENCE_DATA_DIR": str(directory / "data"), "OPENSCIENCE_CONFIG_DIR": str(directory / "config-dir"),
        "XDG_CACHE_HOME": str(directory / "cache"), "XDG_STATE_HOME": str(directory / "state"),
        "XDG_CONFIG_HOME": str(directory / "config-home"), "OPENSCIENCE_DISABLE_AUTOUPDATE": "1",
        "OPENSCIENCE_DISABLE_MODELS_FETCH": "1", "OPENSCIENCE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENSCIENCE_DISABLE_PROJECT_CONFIG": "1", "OPENSCIENCE_DISABLE_LSP_DOWNLOAD": "1"}


def final_report(events):
    """Only the final assistant message after the last tool is a delivered answer."""
    last_tool = max((i for i, row in enumerate(events) if row.get("type") == "tool_use"), default=-1)
    texts = [row for row in events[last_tool + 1:] if row.get("type") == "text"]
    if not texts:
        return ""
    identity = texts[-1]["part"].get("messageID")
    return "\n\n".join(row["part"]["text"] for row in texts if row["part"].get("messageID") == identity)


def codey_worker(root, directory, base_url, case, seed):
    verify_experiment(root)
    from unittest.mock import patch

    from codey.app import headless_runner
    from codey.providers import local_config
    from tests.manual import persistent_research_local as probe
    from tests.manual import research_evidence_local as fixture

    clock = fixture_clock(DEFAULT_CLOCK_FILE)
    limits = experiment_limits(root)
    with ExitStack() as stack:
        save_config = local_config.save_local_config
        stack.enter_context(patch.object(local_config, "save_local_config",
                                         lambda config: save_config(comparison_config(config))))
        stack.enter_context(patch("codey.providers.request_context.clock_context", return_value=clock))
        if case in DIAGNOSTIC:
            original = fixture.stage

            def stage_at_proxy(directory, name, _upstream, model, **kwargs):
                return original(directory, name, base_url, model, **kwargs)

            stack.enter_context(patch.object(fixture, "stage", stage_at_proxy))
            stack.enter_context(patch.object(sys, "argv", ["comparison", "--directory", str(directory),
                "--source-file", str(root / "sqlite-sources.json"), "--case", case, "--layer", "given",
                "--seed", str(seed), "--order-seed", "0", "--submission-mode", "select", "--max-turns", str(limits["turns"])]))
            fixture.main()
            return
        directory.mkdir(parents=True, exist_ok=False)
        docs = documents(root, case)
        for doc in docs:
            doc["sha256"] = hashlib.sha256(doc["text"].encode()).hexdigest()
        write_json(directory / "sources.json", docs)
        task = task_text(root, case)
        (directory / "evidence.md").write_text(task, encoding="utf-8")
        request_type = headless_runner.HeadlessRequest
        stack.enter_context(patch.object(headless_runner, "HeadlessRequest",
            lambda **kwargs: request_type(**{**kwargs, "strict_research": True})))
        with urlopen(UPSTREAM + "/v1/models", timeout=5) as response:
            model = json.load(response)["data"][0]["id"]
        channel = root / "material-channel.json"
        native = channel.exists() and read_json(channel).get("protocol") == "native"
        probe.stage(directory, "research", base_url, model, native=native, live_web=False, question=task,
                    max_turns=limits["turns"], seed=seed, provider_timeout=150, constrained_text=not native)


def run_one(root, framework, case, seed):
    verify_experiment(root)
    name = f"{case}-{seed}-{framework}"
    directory = root / "runs" / name
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    started = time.monotonic()
    limits = experiment_limits(root)
    readiness = backend_ready()
    genkey = f"research-comparison-{uuid4().hex}"
    with InferenceProxy(root / "wire" / f"{name}.jsonl", seed, genkey=genkey, request_limit=limits["requests"], identity_root=root) as proxy, \
         (logs / f"{name}.stdout").open("w", encoding="utf-8") as out, (logs / f"{name}.stderr").open("w", encoding="utf-8") as err:
        if framework == "codey":
            command = [sys.executable, "-X", "utf8", "-m", __spec__.name, "worker", "--root", str(root),
                "--directory", str(directory), "--base-url", proxy, "--case", case, "--seed", str(seed)]
            process = subprocess.Popen(command, stdout=out, stderr=err)
            stdin = None
        else:
            directory.mkdir(parents=True, exist_ok=False)
            prompt = task_text(root, case)
            (directory / "evidence.md").write_text(material_text(root, case), encoding="utf-8")
            with urlopen(UPSTREAM + "/v1/models", timeout=5) as response:
                model = json.load(response)["data"][0]["id"]
            env = openscience_environment(root, directory, proxy, model)
            command = [str(root / "runtime/openscience.exe"), "run", "--workspace", "project", "--format", "json",
                "--model", "localbench/" + model, "--delegation", "off", "--deadline", str(limits["agent_seconds"]),
                "--auto-approve", "--title", "Frozen research comparison"]
            process = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.PIPE, stdout=out,
                                       stderr=err, text=True, encoding="utf-8")
            stdin = prompt
        try:
            process.communicate(input=stdin, timeout=limits["process_seconds"])
            result = {"process_exit_code": process.returncode}
        except subprocess.TimeoutExpired:
            # Kill the evaluation process tree, including any child CLI services.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.communicate(timeout=15)
            result = {"process_exit_code": process.returncode, "runner_timeout": True}
    result.update(framework=framework, case=case, seed=seed, directory=str(directory),
                  elapsed_seconds=round(time.monotonic() - started, 2), semantic_grade="pending_manual_review",
                  backend_before=readiness, generation_key=genkey, limits=limits)
    if framework == "codey" and (directory / "research-result.json").exists():
        result["result"] = read_json(directory / "research-result.json")
    if framework == "openscience":
        events = []
        for line in (logs / f"{name}.stdout").read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        result["result"] = next((row for row in reversed(events) if row.get("type") == "done"), {})
        (directory / "research-report.txt").write_text(final_report(events), encoding="utf-8")
    write_json(directory / "comparison-result.json", result)
    verify_experiment(root)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "worker"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case", choices=[*DIAGNOSTIC, *SYNTHESIS])
    parser.add_argument("--seed", type=int, default=74)
    parser.add_argument("--framework", choices=["codey", "openscience"])
    parser.add_argument("--suite", choices=["all", "diagnostic", "synthesis"], default="all")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "worker":
        codey_worker(root, args.directory, args.base_url, args.case, args.seed)
        return
    (root / "runs").mkdir(exist_ok=True)
    freeze_experiment(root)
    index_path = root / "matrix.json"
    previous = read_json(index_path) if index_path.exists() else []
    cases = [args.case] if args.case else (list(DIAGNOSTIC) if args.suite == "diagnostic" else
            list(SYNTHESIS) if args.suite == "synthesis" else [*DIAGNOSTIC, *SYNTHESIS])
    for case_index, case in enumerate(cases):
        for seed in ([args.seed] if args.case else SEEDS):
            frameworks = [args.framework] if args.framework else (
                ["codey", "openscience"] if (case_index + seed) % 2 == 0 else ["openscience", "codey"])
            for framework in frameworks:
                if any((r["framework"], r["case"], r["seed"]) == (framework, case, seed) for r in previous):
                    continue
                print("START", framework, case, seed, flush=True)
                result = run_one(root, framework, case, seed)
                previous.append(result)
                write_json(index_path, previous)
                print("END", framework, case, seed, result["process_exit_code"], result["elapsed_seconds"], flush=True)


if __name__ == "__main__":
    main()
