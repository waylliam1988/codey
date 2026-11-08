"""Pre-change shell/web baseline for Solar Mini4 and local KoboldCpp Gemma4.

Separately labelled scopes: production host probes, controlled model
receipt replays (candidate receipts are NOT production fixes), and the existing
real HTTP shell-approval entry, plus optional live webpage acquisition. Baseline failures are findings, not a reason to
change production code while collecting evidence. No user settings are saved.

Run: python tools/shell_web_diagnostic_gate.py --run-dir .e2e-artifacts/shell-web-baseline
Offline: add --only offline. Default: both models, one repetition, CMD and pwsh.
Exit 0: all selected checks passed; 1: gaps, model errors or missing coverage.
Provider payloads and replies are local artifacts; authorization headers are not saved.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import local_model_gate_attempts as attempts

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("offline", "replay", "e2e", "web")
MODEL_NAMES = ("solar", "gemma")


def emit(row: dict) -> None:
    # ASCII JSON also works in legacy redirected Windows consoles.
    print(json.dumps(row, ensure_ascii=True), flush=True)


def current_receipt(text: str, *, code: int, truncated: bool = False) -> str:
    """Render the production execution receipt inside its delivery envelope."""
    from codey.operations.kernel_prompt import _result_context
    from codey.runtime.core.execution_receipt import ExecutionReceipt
    from codey.runtime.core.models import ToolCall, ToolResult

    receipt = ExecutionReceipt("exit", code, text, truncated=truncated).bounded()
    result = ToolResult(ToolCall("shell", {"command": "fixture-command"}, "probe-call"),
                        receipt.render(), ok=True, truncated=receipt.truncated, audit={"exit_code": code})
    session = SimpleNamespace(_memory_results={"result_probe": result})
    return _result_context(result, session)


def baseline_receipt(text: str) -> str:
    """Frozen pre-fix evidence: audit exit code was absent from initial text."""
    return (text + '\n\nResult metadata:\n{"result_ref":"result_probe","tool":"shell","ok":true}\n'
            'For read_tool_result only, copy the complete result_ref including its prefix.')


def page_fixture(marker: str) -> str:
    return ('<html><head><link rel="icon" href="data:image/png;base64,' + "A" * 115_000
            + f'"><title>Fixture Gateway {marker}</title></head><body><h1>Fixture Gateway {marker}</h1>'
            '<p>This fixture describes an AI API gateway for model API access.</p></body></html>')


def offline_probes(shell_names: list[str]) -> list[dict]:
    from codey.app.shell_service import ShellExecutionTicket, execute_shell_ticket
    from codey.env_names import SHELL_EXECUTABLE_ENV
    from codey.policies.command_line import resolve_shell
    from codey.policies.shell_analysis import analyze_shell
    from codey.policies.task_policy import build_task_policy
    from codey.research.extract import _usable_fetch_page_text, extract_text, extract_title
    from tools.shell_matrix_gate import ShellMatrixContext

    rows = []
    with tempfile.TemporaryDirectory(prefix="codey-shell-web-probe-") as temporary:
        for name in shell_names:
            try:
                shell = resolve_shell(environ={**os.environ, SHELL_EXECUTABLE_ENV: name})
                native = 'python -c "import sys; sys.exit(23)"'
                commands = [("native_exit", native, 1 if shell.kind == "powershell" else 23),
                            ("chinese_output", 'python -c "print(\'中文\')"', None)]
                if shell.kind == "powershell":
                    commands += [("native_exit_then_output", native + "; Write-Output after", 0),
                                 ("explicit_native_exit", native + "; exit $LASTEXITCODE", 23)]
                for case, command, expected_code in commands:
                    context = ShellMatrixContext()
                    try:
                        result = execute_shell_ticket(context, ShellExecutionTicket(
                            command, Path(temporary), 0, 20, 4000, shell, analyze_shell(command, shell)))
                    finally:
                        context.lifecycle.close()
                    ok = ("\u4e2d\u6587" in result.get("output", "") if expected_code is None
                          else result.get("exit_code") == expected_code)
                    rows.append({"scope": "production_host", "case": case, "shell": name,
                                 "shell_details": shell.to_payload(), "ok": ok,
                                 "expected_exit": expected_code, "result": result})
            except Exception as exc:
                rows.append({"scope": "production_host", "case": "shell_discovery", "shell": name,
                             "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    text = current_receipt("(no output)", code=23)
    rows.append({"scope": "production_formatter", "case": "exit_code_visible", "ok": "23" in text,
                 "audit_exit_code": 23, "model_receipt": text})
    for requested in ((), ("web.read",)):
        submission = SimpleNamespace(project=str(ROOT), requested_capabilities=requested)
        policy = build_task_policy(submission, task_kind="chat")
        rows.append({"scope": "production_policy", "case": "chat_web_capability",
                     "requested": list(requested), "grants": sorted(policy.grants),
                     "ok": policy.allows("web.read") == bool(requested),
                     "interpretation": "Records current policy; does not change authorization."})
    html = page_fixture("OFFLINE")
    title = extract_title(html)
    rows.append({"scope": "production_extractor", "case": "late_title", "ok": title == "Fixture Gateway OFFLINE",
                 "title": title, "title_offset": html.index("<title>"),
                 "title_in_first_20k": "<title>" in html[:20_000], "text": extract_text(html)})
    challenge = extract_text("<html><body>Verify you are human. Enable JavaScript.</body></html>")
    rows.append({"scope": "production_extractor", "case": "challenge_not_evidence",
                 "ok": not _usable_fetch_page_text(challenge), "text": challenge})
    return rows


def replay_jobs() -> list[dict]:
    """Controlled input ablations, never represented as live tool execution."""
    from codey.research.extract import extract_text, extract_title

    jobs = []
    instruction = ("\u8fd9\u662f\u5de5\u5177\u56de\u6267\u56de\u653e\u5b9e\u9a8c\uff0c\u4e0d\u662f\u65b0\u7684\u8054\u7f51\u6216\u6267\u884c\u8bf7\u6c42\u3002\u53ea\u4f9d\u636e\u6240\u7ed9\u56de\u6267\u56de\u7b54\uff0c\u4e0d\u731c\u6d4b\u672a\u663e\u793a\u7684\u4fe1\u606f\u3002"
                   "\u53ea\u8fd4\u56de\u4e00\u4e2aJSON\u5bf9\u8c61\uff0c\u4e0d\u8981Markdown\u3002\n")
    failure_question = ('\u7528\u6237\u60f3\u77e5\u9053\u547d\u4ee4\u662f\u5426\u6210\u529f\u4ee5\u53ca\u4e3a\u4f55\u6ca1\u6709\u7f51\u9875\u3002\u8fd4\u56de\u5b57\u6bb5 exit_code\uff08\u6574\u6570\u6216null\uff09\u3001'
                        'cause\uff08local_command_error/network_error/unknown\uff09\u3001website_verified\uff08\u5e03\u5c14\uff09\u3002\n')
    for variant, receipt, expected in (
        ("current", baseline_receipt("(no output)"),
         {"exit_code": None, "cause": "unknown", "website_verified": False}),
        ("candidate", json.dumps({"call_status": "completed", "exit_code": 2,
                                  "stderr": "curl: option ---HEAD---: is unknown", "stdout": ""}),
         {"exit_code": 2, "cause": "local_command_error", "website_verified": False}),
    ):
        jobs.append({"case": "failure_receipt", "variant": variant, "expected": expected,
                     "prompt": instruction + failure_question + receipt,
                     "information_available": variant == "candidate"})
    truncation_question = ('\u8fd4\u56de\u5b57\u6bb5 output_complete\u3001full_output_recoverable\uff0c\u503c\u4e3a\u5e03\u5c14\u6216null\u3002'
                           'output_complete\u8868\u793a\u662f\u5426\u5b8c\u6574\u91c7\u96c6\u4e86\u672c\u6b21\u8f93\u51fa\uff1bfull_output_recoverable\u8868\u793a\u80fd\u5426\u901a\u8fc7read_tool_result\u8bfb\u56de\u672c\u6b21\u6267\u884c\u7684\u5168\u90e8\u8f93\u51fa\uff0c\u4e0d\u5305\u62ec\u91cd\u65b0\u6267\u884c\u547d\u4ee4\u3002'
                           '\u56de\u6267\u6ca1\u6709\u8bf4\u660e\u7684\u5b57\u6bb5\u586bnull\u3002\n')
    for variant, receipt, expected in (
        ("current", baseline_receipt("HEAD\nTAIL"),
         {"output_complete": None, "full_output_recoverable": None}),
        ("candidate", json.dumps({"exit_code": 0, "capture_truncated": True,
                                  "omitted_bytes": 100_000, "full_output_retained": False,
                                  "recovery": {"read_tool_result_full_available": False,
                                               "reason": "capture_discarded_middle"},
                                  "output": "HEAD\n[omitted]\nTAIL"}),
         {"output_complete": False, "full_output_recoverable": False}),
    ):
        jobs.append({"case": "truncation_receipt", "variant": variant, "expected": expected,
                     "prompt": instruction + truncation_question + receipt,
                     "information_available": variant == "candidate"})
    marker = uuid.uuid4().hex[:10]
    html = page_fixture(marker)
    web_question = ('\u8fd4\u56de\u5b57\u6bb5 page_title\uff08\u5b57\u7b26\u4e32\u6216null\uff09\u3001gateway_evidence\uff08\u5e03\u5c14\uff09\u3002'
                    '\u53ea\u6709\u5b9e\u9645\u6807\u9898\u6216\u6b63\u6587\u663e\u793aAPI gateway\u65f6\uff0cgateway_evidence\u624d\u4e3atrue\u3002\n')
    for variant, receipt, expected in (
        ("current", html[:20_000], {"page_title": None, "gateway_evidence": False}),
        ("candidate", json.dumps({"url": "https://fixture.example.test/", "http_status": 200,
                                  "title": extract_title(html), "text": extract_text(html)}),
         {"page_title": f"Fixture Gateway {marker}", "gateway_evidence": True}),
    ):
        jobs.append({"case": "web_content", "variant": variant, "expected": expected,
                     "prompt": instruction + web_question + receipt,
                     "information_available": variant == "candidate"})
    for shell in ("cmd", "pwsh"):
        jobs.append({"case": "shell_dialect", "variant": shell, "expected": None,
                     "marker": "DIALECT_" + uuid.uuid4().hex[:10], "shell": shell})
    return jobs


def parse_json_reply(reply: str) -> dict:
    value = reply.strip()
    if value.startswith("```json\n") and value.endswith("\n```"):
        value = value[8:-4]
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("reply must be a JSON object")
    return result


def exact_fields(actual: dict, expected: dict) -> bool:
    return all(key in actual and type(actual[key]) is type(value) and actual[key] == value
               for key, value in expected.items())


def replay_worker(target: attempts.GateTarget, job: dict, directory: Path) -> dict:
    from codey.env_names import SHELL_EXECUTABLE_ENV
    from codey.policies.command_line import resolve_shell
    from codey.policies.shell_analysis import analyze_shell
    from codey.runtime.core.cancellation import start_process, wait_process

    prompt = job.get("prompt", "")
    shell = None
    if job["case"] == "shell_dialect":
        shell = resolve_shell(environ={**os.environ, SHELL_EXECUTABLE_ENV: job["shell"]})
        prompt = (f"Execution host: Windows. Shell: {shell.executable}, version {shell.version}. "
                  f"Generate one shell command that prints exactly {job['marker']} on its own line. "
                  "No file writes, networking, external programs, pipes or redirections. "
                  'CMD: use echo. PowerShell: use Write-Output. Return only JSON {"command":"..."}.')
    attempts.write_json(directory / "replay-input.json", {**job, "prompt": prompt})
    provider = attempts.make_provider(target, directory)
    started = time.monotonic()
    try:
        with attempts.record_api_generations(directory / "provider.jsonl"):
            reply = provider.send(prompt, timeout=target.request_timeout, output_token_limit=512)
    finally:
        provider.close()
    (directory / "reply.txt").write_text(reply, encoding="utf-8")
    actual = parse_json_reply(reply)
    if shell is None:
        ok = exact_fields(actual, job["expected"])
        proof = {"expected": job["expected"], "actual": actual}
    else:
        command = actual.get("command")
        # Only these exact harmless grammars are executed. Model-supplied code
        # is never admitted just because it passed a heuristic safety check.
        marker = job["marker"]
        allowed = ({f"echo {marker}", f"@echo {marker}"} if shell.kind == "cmd" else
                   {f"Write-Output '{marker}'", f'Write-Output "{marker}"', f"Write-Output {marker}"})
        if command not in allowed:
            return {"ok": False, "failure_stage": "command_outside_probe_grammar", "actual": actual}
        with tempfile.TemporaryDirectory(prefix="codey-dialect-") as temporary:
            argv = shell.invocation(command)
            proc, owner = start_process(argv, cwd=Path(temporary))
            result = wait_process(proc, owner, argv, 15, capture_limit_bytes=4000)
        ok = result.returncode == 0 and result.stdout.strip() == marker
        proof = {"actual": actual, "analysis": analyze_shell(command, shell).to_payload(),
                 "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    return {"ok": ok, "failure_stage": "" if ok else "model_contract_mismatch",
            "seconds": round(time.monotonic() - started, 3), **proof}


def run_replay(target: attempts.GateTarget, job: dict, directory: Path, timeout: float) -> dict:
    from codey.runtime.core.cancellation import start_process, wait_process

    directory.mkdir(parents=True, exist_ok=False)
    attempts.write_json(directory / "job.json", {"target": asdict(target), "job": job})
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", str(directory)]
    try:
        proc, owner = start_process(command, cwd=ROOT)
        completed = wait_process(proc, owner, command, timeout, capture_limit_bytes=64_000)
        (directory / "worker.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        result_path = directory / "worker-result.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {
            "ok": False, "failure_stage": "worker_error", "exit_code": completed.returncode}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "failure_stage": "case_timeout", "timeout": timeout}
    return {"scope": "controlled_model_replay", "case": job["case"], "variant": job["variant"],
            "information_available": job.get("information_available"), **result, "artifacts": str(directory)}


def select_target(name: str, timeout: float, base_url: str) -> attempts.GateTarget:
    if name == "solar":
        from codey.providers.cline.connection import capture_selection
        from codey.providers.cline.identity import INFERENCE_URL

        selection = capture_selection({"model": "cline-free/solar-mini4"})
        selection = replace(selection, stream=False, output_tokens=min(768, selection.output_tokens or 768))
        return attempts.GateTarget(INFERENCE_URL, selection.model_id, selection.context_window_tokens,
            selection.context_reserve_tokens, selection.context_keep_recent_tokens, provider_id="cline",
            api_selection=selection.to_payload(), request_timeout=timeout)
    from codey.providers.local_discovery import probe_local_endpoint_detail

    endpoint, reason = probe_local_endpoint_detail(base_url, timeout=5)
    if endpoint is None:
        raise RuntimeError(f"KoboldCpp preflight: {reason}")
    matches = [model for model in endpoint.models if "gemma4" in model.lower() or "gemma-4" in model.lower()]
    if len(matches) != 1:
        raise ValueError("KoboldCpp must expose exactly one Gemma4 model; will not substitute another model")
    return attempts.GateTarget(endpoint.base_url, matches[0], 32768, 8192, 12000, request_timeout=timeout)


def receipt_delivery_audit(directory: Path) -> dict:
    """Look at actual outbound model input, separately from the UI event."""
    path = directory / "provider.jsonl"
    if not path.is_file():
        return {"receipt_seen": False, "initial_exit_code_visible": False, "eventual_exit_code_visible": False}
    shell_texts, all_texts = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("type") != "request":
            continue
        for message in row.get("payload", {}).get("messages", []):
            content = str(message.get("content", ""))
            if message.get("role") == "tool":
                all_texts.append(content)
                if re.search(r'"tool"\s*:\s*"shell"', content) and "SHELL_GATE_RESULT" in content:
                    shell_texts.append(content)
            elif message.get("role") == "user":
                # Compatibility protocol has text result blocks, not role=tool.
                blocks = re.split(r"\[result: ([a-z_]+)\]\n", content)
                for index in range(1, len(blocks) - 1, 2):
                    body = blocks[index + 1].split("\n\nContinue with", 1)[0]
                    all_texts.append(body)
                    if blocks[index] == "shell" and "SHELL_GATE_RESULT" in body:
                        shell_texts.append(body)

    def contains_exit(text: str) -> bool:
        return re.search(r'"exit_code"\s*:\s*-?\d+|Process exited with code -?\d+|Exit code: -?\d+', text) is not None

    return {"receipt_seen": bool(shell_texts),
            "initial_exit_code_visible": bool(shell_texts) and contains_exit(shell_texts[0]),
            "eventual_exit_code_visible": any(contains_exit(text) for text in all_texts),
            "initial_receipt": shell_texts[0] if shell_texts else "",
            "model_result_excerpt": "\n".join(all_texts)[-5000:]}


def approval_worker(target: attempts.GateTarget, directory: Path) -> dict:
    """Use production admission/approval for native AND compatibility tools."""
    from codey.policies.command_line import resolve_shell
    from tools.local_model_gate_shell import GateHTTPClient, gate_application, probe_command, shell_prompt, wait_for_run

    started = time.monotonic()
    with gate_application(target, directory) as (httpd, state, events):
        client = GateHTTPClient(httpd)
        selection = {"model": target.model}
        if target.provider_id == "local":
            selection["base_url"] = target.base_url
        submitted = client.post("/api/run", {
            "session_id": "gate-shell", "input": {"kind": "start_task", "text": shell_prompt("shell_failure")},
            "provider": target.provider_id, "model_selection": selection, "intent": "chat", "max_turns": 8})
        run_id = submitted["run_id"]
        terminal = wait_for_run(client, run_id, target.request_timeout)
        if terminal.get("stop_reason") != "approval":
            return {"ok": False, "failure_stage": "no_approval_request", "terminal": terminal}
        pending = state.pending_shell_approvals()
        if len(pending) != 1:
            raise AssertionError("expected exactly one approval")
        approval_id, record = next(iter(pending.items()))
        if record.get("command") != probe_command("shell_failure"):
            # Never approve a model's altered command.
            client.post("/api/shell_approval", {"id": approval_id, "approved": False})
            return {"ok": False, "failure_stage": "model_changed_probe_command"}
        client.post("/api/shell_approval", {"id": approval_id, "approved": True})
        deadline = time.monotonic() + target.request_timeout
        final = None
        while time.monotonic() < deadline:
            snapshot = client.state()
            terminals = [event for event in events if event.get("type") == "task_done"
                         and event.get("run_id") == run_id and event.get("stop_reason") != "approval"]
            if not snapshot.get("busy") and terminals:
                final = terminals[-1]
                break
            time.sleep(0.05)
        executions = [event for event in events if event.get("type") == "shell_result"
                      and event.get("run_id") == run_id]
        audit = receipt_delivery_audit(directory)
        expected_code = 1 if resolve_shell().kind == "powershell" else 7
        execution_ok = (len(executions) == 1 and executions[0].get("exit_code") == expected_code
                        and "SHELL_GATE_RESULT" in executions[0].get("output", ""))
        continued = final is not None and final.get("stop_reason") == "done"
        return {"ok": execution_ok and continued and audit["initial_exit_code_visible"],
                "execution_matches_shell_exit": execution_ok, "continuation_completed": continued,
                "scope": "production_http_approval", "expected_shell_exit": expected_code,
                "failure_stage": ("continuation_incomplete" if not continued else
                                  "shell_exit_mismatch" if not execution_ok else
                                  "initial_receipt_missing_exit" if not audit["initial_exit_code_visible"] else ""),
                "protocol_window_present": bool(record.get("window_ref")), "executions": executions,
                "terminal": final, "receipt_delivery": audit,
                "seconds": round(time.monotonic() - started, 3)}


def web_worker(target: attempts.GateTarget, job: dict, directory: Path) -> dict:
    """Unmocked source acquisition through the production headless task entry."""
    from codey.app.headless_runner import HeadlessRequest, run_headless
    from codey.env_names import NATIVE_TOOLS_ENV

    events = []

    def observe(row: dict) -> None:
        events.append(row)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs):
        if provider_id != target.provider_id:
            raise ValueError("diagnostic will not fail over to another provider")
        return attempts.make_provider(target, directory)

    task = (f"\u8bf7\u6253\u5f00 {job['url']} \u770b\u770b\u8fd9\u4e2a\u7f51\u7ad9\u662f\u5e72\u561b\u7684\u3002\u4ee5\u5b9e\u9645\u8bfb\u53d6\u7684\u9875\u9762\u4e3a\u4f9d\u636e\uff0c\u8bfb\u4e0d\u5230\u5c31\u660e\u786e\u8bf4\u660e\u3002"
            '这是单页读取验收：只读取指定首页一次，不扩展其他页面；收到结果后直接用done结束。'
            '\u6700\u7ec8\u7528done\u7684summary\u8fd4\u56de\u88f8JSON\uff1a'
            '{"site_type":"api_gateway\u6216unknown","verified":true\u6216false,"sources":["\u5b9e\u9645\u8bfb\u53d6\u7684URL"],"reason":"\u4e2d\u6587\u8bf4\u660e"}\u3002'
            '\u6ca1\u6709\u8bfb\u5230\u9875\u9762\u65f6\uff0csite_type\u4e3aunknown\uff0cverified\u4e3afalse\uff0csources\u4e3a\u7a7a\u6570\u7ec4\u3002')
    selection = {"model": target.model}
    if target.provider_id == "local":
        selection["base_url"] = target.base_url
    if job["variant"] == "capability_absent":
        task = (f"不要联网，不要打开 {job['url']}。无法验证该网站，请结束任务。"
                '用done的summary返回裸JSON：{"site_type":"unknown","verified":false,"sources":[],"reason":"未联网"}')
    with (
        tempfile.TemporaryDirectory(prefix="codey-web-entry-") as temporary,
        patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}),
        attempts.isolate_gate_secondary_models(target),
        attempts.record_api_generations(directory / "provider.jsonl"),
    ):
        run_headless(HeadlessRequest(
            project=None, task=task, intent="chat", provider_id=target.provider_id, max_turns=6,
            state_home=Path(temporary), model_selection=selection,
            requested_capabilities=(),
            project_changes_required=False), emit_jsonl=observe, connect_provider=connect)
    return analyze_web_artifacts(directory, job)


def analyze_web_artifacts(directory: Path, job: dict) -> dict:
    """Analyze saved evidence without asking a model or fetching a URL again."""
    event_path = directory / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
    text = terminal.get("summary", "")
    actual, format_error = {}, ""
    # A stopped/max-turns terminal is runtime status, not a model JSON reply.
    if terminal.get("stop_reason") == "done":
        try:
            actual = parse_json_reply(text)
        except ValueError as exc:
            format_error = str(exc)
    opened = [row for row in events if row.get("type") == "tool" and row.get("ok") is True
              and row.get("tool_name") in {"open_url", "open_result", "open_hit", "reopen_source"}]
    available = {row.get("tool_name") for row in events if row.get("type") == "tool"}
    for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("type") == "request":
            payload = row.get("payload", {})
            available.update(tool.get("function", {}).get("name") for tool in payload.get("tools", []))
            for message in payload.get("messages", []):
                match = re.search(r"Visible tools: ([^\n]+)", str(message.get("content", "")))
                if match:
                    available.update(name.strip() for name in match[1].split(","))
    available.discard(None)
    honest_without_page = exact_fields(actual, {"site_type": "unknown", "verified": False, "sources": []})
    verified = (actual.get("site_type") == "api_gateway" and actual.get("verified") is True
                and isinstance(actual.get("sources"), list) and job["url"] in actual["sources"])
    matched_opens = [row for row in opened if str(row.get("path", "")).rstrip("/") == job["url"].rstrip("/")]
    ok = bool(matched_opens) and verified and terminal.get("stop_reason") == "done"
    if job.get("variant") == "capability_absent":
        ok = not opened and "open_url" not in available and honest_without_page and terminal.get("stop_reason") == "done"
    blocked_homepage = any("low_value_landing_page_url" in str(row.get("result", "")) for row in events)
    stage = ("" if ok else "source_filter_blocked" if blocked_homepage else
             "web_capability_absent" if "open_url" not in available else
             "model_response_format" if format_error else "web_task_not_verified")
    return {"ok": ok, "failure_stage": stage,
            "stop_reason": terminal.get("stop_reason"), "actual": actual,
            "raw_summary": text, "summary_format_error": format_error,
            "web_tool_available": "open_url" in available, "available_tools": sorted(available),
            "successful_opens": len(opened), "honest_without_page": honest_without_page,
            "homepage_filter_blocked": blocked_homepage,
            "headless_shell_denied": any(row.get("type") == "shell_rejected" for row in events),
            "url": job["url"], "live_source_events": [row for row in events if row.get("type") == "tool"]}


def reanalyze(directory: Path) -> int:
    """Keep the original report intact; repair analysis from its recorded facts."""
    directory = directory.resolve()
    source = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    rows = []
    for row in source["results"]:
        if row.get("scope") == "production_web_entry":
            case_dir = Path(row["artifacts"]).resolve()
            case_dir.relative_to(directory)
            config = json.loads((case_dir / "job.json").read_text(encoding="utf-8"))
            facts = analyze_web_artifacts(case_dir, config["job"])
            row = {**row, **facts, "original_failure_stage": row.get("failure_stage"),
                   "original_error": row.get("error"), "error": ""}
        rows.append(row)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    report = summarize(rows, scopes=source["requested_scopes"], models=source["requested_models"],
                       repeat=source["repeat"], shells=manifest["arguments"]["shells"])
    report["analysis_of"] = str(directory / "summary.json")
    report["analysis_note"] = "Reclassified saved web runtime terminals; no model or network calls were repeated."
    output = directory / "summary.reanalyzed.json"
    attempts.write_json(output, report)
    emit({"summary": str(output), "matrix_complete": report["matrix_complete"], "ok": report["ok"]})
    return 0 if report["matrix_complete"] else 1


def summarize(rows: list[dict], *, scopes: list[str], models: list[str], repeat: int,
              shells: list[str] | None = None) -> dict:
    blocked = [row for row in rows if row.get("scope") == "model_preflight" and not row.get("ok")]
    replays = [row for row in rows if row.get("scope") == "controlled_model_replay"]
    expected = len(models) * repeat * 8 if "replay" in scopes else 0
    expected_e2e = len(models) * repeat * len(shells or ["cmd", "pwsh"]) if "e2e" in scopes else 0
    completed_e2e = sum(row.get("scope") == "production_http_approval" for row in rows)
    expected_web = len(models) * repeat * 2 if "web" in scopes else 0
    completed_web = sum(row.get("scope") == "production_web_entry" for row in rows)
    expected_slots = set()
    for label in models:
        for repetition in range(1, repeat + 1):
            if "replay" in scopes:
                pairs = [(case, variant) for case in ("failure_receipt", "truncation_receipt", "web_content")
                         for variant in ("current", "candidate")]
                pairs += [("shell_dialect", variant) for variant in ("cmd", "pwsh")]
                expected_slots.update(("controlled_model_replay", label, repetition, case, variant)
                                      for case, variant in pairs)
            if "e2e" in scopes:
                expected_slots.update(("production_http_approval", label, repetition, "approval", shell)
                                      for shell in (shells or ["cmd", "pwsh"]))
            if "web" in scopes:
                expected_slots.update(("production_web_entry", label, repetition, "web_entry", variant)
                                      for variant in ("capability_absent", "explicit_web"))
    observed = Counter((row.get("scope"), row.get("model_label"), row.get("repetition"),
                        row.get("case"), row.get("variant")) for row in rows
                       if row.get("scope") in {"controlled_model_replay", "production_http_approval", "production_web_entry"})
    missing = expected_slots - observed.keys()
    duplicates = [slot for slot, count in observed.items() if count != 1]
    unexpected = observed.keys() - expected_slots
    matrix_complete = not (missing or duplicates or unexpected)
    return {"ok": (bool(rows) and all(row.get("ok") is True for row in rows)
                   and matrix_complete),
            "requested_scopes": scopes, "requested_models": models, "repeat": repeat,
            "blocked_models": [row["model_label"] for row in blocked],
            "expected_replays": expected, "completed_replays": len(replays),
            "expected_e2e": expected_e2e, "completed_e2e": completed_e2e,
            "expected_web": expected_web, "completed_web": completed_web,
            "matrix_complete": matrix_complete, "missing_slots": sorted(missing, key=str),
            "duplicate_slots": sorted(duplicates, key=str), "unexpected_slots": sorted(unexpected, key=str),
            "passed_checks": sum(row.get("ok") is True for row in rows), "checks": len(rows),
            "scope_limits": ["Candidate receipts are controlled inputs, not deployed fixes.",
                             ("Live web entry covers the requested URL only; search relevance is not measured."
                              if "web" in scopes else "No live website/search end-to-end coverage is claimed."),
                             "A single repetition is diagnostic, not a model ranking.",
                             "Shell dialect prompts include guidance; this is not a blind capability benchmark.",
                             "Nonzero baseline exit can indicate reproduced product gaps."],
            "results": rows}


def worker(directory: Path) -> int:
    config = json.loads((directory / "job.json").read_text(encoding="utf-8"))
    try:
        target = attempts.GateTarget(**config["target"])
        if config["job"]["case"] == "approval":
            result = approval_worker(target, directory)
        elif config["job"]["case"] == "web_entry":
            result = web_worker(target, config["job"], directory)
        else:
            result = replay_worker(target, config["job"], directory)
    except Exception as exc:
        result = {"ok": False, "failure_stage": "provider_or_harness_error",
                  "error_class": type(exc).__name__, "error": str(exc)}
    attempts.write_json(directory / "worker-result.json", result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--models", nargs="+", choices=MODEL_NAMES, default=list(MODEL_NAMES))
    parser.add_argument("--only", nargs="+", choices=SCOPES, default=["offline", "replay", "e2e"])
    parser.add_argument("--shells", nargs="+", choices=("cmd", "powershell", "pwsh"), default=["cmd", "pwsh"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--url", default="https://api.lihe.chat/", help="URL for optional --only web")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--reanalyze", type=Path, help="reclassify saved web terminals without model/network calls")
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if args.reanalyze:
        return reanalyze(args.reanalyze)
    if args.run_dir is None or args.timeout <= 0 or args.repeat < 1:
        parser.error("--run-dir, positive --timeout and positive --repeat are required")
    for option in ("models", "only", "shells"):
        if len(getattr(args, option)) != len(set(getattr(args, option))):
            parser.error(f"--{option} must not contain duplicates")
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    rows = []
    import hashlib

    manifest = {"git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "python": sys.version, "script": str(Path(__file__).resolve()),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "initial_worktree": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
                "production_changes": False, "arguments": {k: str(v) if isinstance(v, Path) else v
                                                           for k, v in vars(args).items()}}
    attempts.write_json(directory / "manifest.json", manifest)

    def record(row: dict) -> None:
        rows.append(row)
        attempts.write_json(directory / "summary.json", summarize(rows, scopes=args.only, models=args.models,
                                                                  repeat=args.repeat, shells=args.shells))
        emit({k: v for k, v in row.items() if k in {
            "scope", "case", "variant", "model_label", "shell", "ok", "failure_stage", "error"}})

    if "offline" in args.only:
        for row in offline_probes(args.shells):
            record(row)
    if set(args.only) & {"replay", "e2e", "web"}:
        jobs = replay_jobs()
        for label in args.models:
            try:
                target = select_target(label, args.timeout, args.kobold_base_url)
                record({"scope": "model_preflight", "model_label": label, "ok": True,
                        "model": target.model, "api_protocol": target.api_protocol})
            except Exception as exc:
                record({"scope": "model_preflight", "model_label": label, "ok": False,
                        "error": f"{type(exc).__name__}: {exc}"})
                continue
            for repetition in range(1, args.repeat + 1):
                if "replay" in args.only:
                    # Alternate paired order across repetitions to reduce order bias.
                    ordered = jobs if repetition % 2 else list(reversed(jobs))
                    for job in ordered:
                        case_dir = directory / f"{label}-{repetition}-{job['case']}-{job['variant']}"
                        emit({"starting": case_dir.name})
                        row = run_replay(target, job, case_dir, args.timeout)
                        record({**row, "model_label": label, "model": target.model, "repetition": repetition})
                if "e2e" in args.only:
                    from codey.env_names import SHELL_EXECUTABLE_ENV

                    for shell in args.shells:
                        case_dir = directory / f"{label}-{repetition}-e2e-{shell}"
                        emit({"starting": case_dir.name})
                        with patch.dict(os.environ, {SHELL_EXECUTABLE_ENV: shell}):
                            result = run_replay(target, {"case": "approval", "variant": shell}, case_dir, args.timeout)
                        record({**result, "scope": "production_http_approval", "model_label": label,
                                "model": target.model, "shell": shell, "repetition": repetition})
                if "web" in args.only:
                    for variant in ("capability_absent", "explicit_web"):
                        case_dir = directory / f"{label}-{repetition}-web-{variant}"
                        emit({"starting": case_dir.name})
                        result = run_replay(target, {"case": "web_entry", "variant": variant, "url": args.url},
                                            case_dir, args.timeout)
                        record({**result, "scope": "production_web_entry", "model_label": label,
                                "model": target.model, "repetition": repetition})
    summary = summarize(rows, scopes=args.only, models=args.models, repeat=args.repeat, shells=args.shells)
    attempts.write_json(directory / "summary.json", summary)
    emit({"summary": str(directory / "summary.json"), "ok": summary["ok"],
          "passed": summary["passed_checks"], "checks": summary["checks"]})
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
