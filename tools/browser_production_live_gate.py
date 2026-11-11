"""KoboldCpp + real Chromium through production entry, policy, CodeMode and receipts.

Only the provider connection and source fixtures are injected. This gate never
registers tools, patches authorization/ANSWER_TOOLS, or implements an executor.
It exercises a controlled page, not general autonomous website reliability.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import Route

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations import kernel_protocol
from codey.operations.kernel_protocol import KernelSessionView, TurnSnapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.policies.tool_requirement import require_tool_success
from codey.providers.local_discovery import probe_local_endpoint_detail
from codey.research.browser_search import BrowserSearchProvider
from codey.storage.input_attachments import InputAttachmentStore
from codey.types.json_value import decode_json_object
from tools.completion_artifact_audit import rejection_audit, tool_receipts
from tools.completion_explanation_ab import (
    BINDINGS,
    SCHEMA,
    ExperimentProvider,
    Target,
    explanation_checks,
    final_checks,
)

URL = "https://example.com/production-browser-record"


def legacy_urls_match(answer: str) -> bool:
    urls = [value.rstrip(".,;:!?") for value in re.findall(r'https?://[^\s`*<>"\[\]()，。；、！？）]+', answer)]
    return bool(urls) and all(value == URL for value in urls)


DOWNLOAD_BYTES = b"DOWNLOAD_BYTES_93\n"
HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Production browser fixture</title></head><body>
<label>Locality<input id="city"></label>
<button onclick="if(document.querySelector('#city').value==='福州') {
window.__record={amount:173,code:'QUARTZ_851'};
document.querySelector('article').textContent='福州样本：鱼丸净重为 '+window.__record.amount+' 克。记录校验码 '+window.__record.code+'。';
} else { document.querySelector('article').textContent='请先填写福州'; }">Load record</button>
<article>尚未加载记录。请填写 Locality 并点击 Load record。</article>
<label>Attachment<input type="file" onchange="this.files[0].text().then(t => document.querySelector('#uploaded').textContent=this.files[0].name+':'+t)"></label>
<p id="uploaded">No file selected</p>
<a href="data:application/octet-stream;base64,__DOWNLOAD_DATA__" download="fixture-download.txt">Download fixture</a>
<script>window.__contractToken='SCHEMA_TOKEN_74';</script></body></html>""".replace(
    "__DOWNLOAD_DATA__", base64.b64encode(DOWNLOAD_BYTES).decode("ascii"))


def select_target(timeout: float, base_url: str) -> Target:
    endpoint, reason = probe_local_endpoint_detail(base_url, timeout=5)
    if endpoint is None or len(endpoint.models) != 1:
        raise RuntimeError("KoboldCpp preflight: " + reason)
    return Target(endpoint.base_url, endpoint.models[0], 123456789, timeout)


def run_case(directory: Path, mode: str, target: Target, *, restore_snapshots: bool = False,
             reread: bool = False, stale_edit: bool = False, structured: bool = False,
             upload_file: bool = False, download_file: bool = False, bound_output: bool = False,
             strict_research: bool = False, explanation_output: bool = False,
             stale_ref: bool = False, source_failure: bool = False, fixed_failure: bool = False) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=False)
    provider = BrowserSearchProvider(headless=True)
    recording = ExperimentProvider(target, directory, fixed_acquisition=fixed_failure, acquisition_action={
        "tool": "codemode", "args": {"code": "unwrap(await tools.browser_navigate({url:inputs.url})); "
            "return await tools.browser_read({});", "inputs": {"url": URL}}} if fixed_failure else None)
    events: list[dict[str, object]] = []
    navigations: list[str] = []
    started = time.monotonic()
    snapshot_count = 0
    changed_after_read = False
    result_file: Path | None = None
    build_snapshot = kernel_protocol.build_turn_snapshot

    def restored_snapshot(session: KernelSessionView, *, native: bool = False) -> TurnSnapshot:
        nonlocal snapshot_count
        original = build_snapshot(session, native=native)
        restored = restore_snapshot(snapshot_description(original))
        if restored != original:
            raise AssertionError("snapshot serialization changed the frozen contract")
        snapshot_count += 1
        return restored

    def observe(row: dict[str, object]) -> None:
        nonlocal changed_after_read
        events.append(row)
        if row.get("type") == "tool":
            recording.observed_tools += 1
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        if (stale_edit and not changed_after_read and result_file is not None
                and row.get("type") == "tool" and row.get("tool_name") == "read_file" and row.get("ok") is True):
            result_file.write_text('{"amount": -1, "code": "externally_changed"}\n', encoding="utf-8")
            changed_after_read = True

    def route_document(route: Route) -> None:
        navigations.append(route.request.url)
        route.fulfill(status=403 if source_failure else 200 if route.request.url == URL else 404, content_type="text/html",
                      body="HTTP 403: fixture access denied" if source_failure else HTML if route.request.url == URL else "Fixture URL unavailable")

    def source_factory() -> BrowserSearchProvider:
        return provider

    def connect(provider_id: str, **_kwargs: object) -> ExperimentProvider:
        if provider_id != "local":
            raise ValueError("gate only authorizes the selected KoboldCpp model")
        return recording

    task = (f"这是用户授权的浏览器操作任务。用生产工具打开 {URL}。"
            "通过 codemode 的 tools.browser_navigate、browser_snapshot、browser_fill、browser_click、browser_wait、browser_read 完成操作。"
            "先观察页面，把 Locality 填为福州；填写后旧 ref 失效，需要重新 snapshot，再点击 Load record，等待出现鱼丸，最后 browser_read 当前页。"
            "每一步检查工具结果，不要猜 ref；用 unwrap 获得结构化 snapshot.elements，每项有 ref/role/name。"
            "browser_read 返回字符串，直接 return unwrap(await tools.browser_read({}))，不要再取 .text。"
            "最后报告实际净重和校验码，不要用 snapshot 或点击结果替代 browser_read 正文。不要运行测试或命令。")
    if mode == "research":
        task += "用 submit_research_answer 提交完整答案，引用实际读到的 [eN] 原文。"
    elif mode == "hybrid":
        task += "然后读取项目的 result.json，将 amount 改为实测克数，code 改为实测校验码，再用 done 报告。"
    else:
        task += "最后用 done 报告，不修改文件。"
    if stale_ref:
        task += ("额外测试：首次 snapshot 保存 Load record 按钮 ref；填入城市后，先尝试一次这个旧 ref 的 browser_click，"
                 "预期得到 stale_ref 拒绝。然后重新 snapshot 取得新 ref，再继续正常操作。不要跳过这次预期失败。")
    if reread:
        task += "另一个验收要求：导航后、填写之前先 browser_read 读取未加载状态；加载后再次 browser_read，以后一次正文为准。"
    if structured or explanation_output:
        task += ("另外用 browser_eval 读取 window.__contractToken，取得返回对象的 value；"
                 "用 browser_tab_list 获取当前绑定标签页的 URL。最终报告脚本 token 和当前标签页 URL；脚本 token 是动作结果，不要作为来源引用。")
    required = {"browser_navigate", "browser_snapshot", "browser_fill", "browser_click", "browser_wait", "browser_read"}
    if structured or explanation_output:
        required.update({"browser_eval", "browser_tab_list"})
    bindings: tuple[str, ...] = BINDINGS if explanation_output else ()
    if explanation_output:
        task += "最终 done.summary 只填自然语言解释，分两段，不重复 URL 或 token；应用从正式回执填入精确 JSON 字段。"
    if bound_output:
        task += ("完成 browser_read 后，用 browser_eval 的 code=window.__record 读取该页面同一记录的结构化对象。"
                 "这两个字段由应用从该回执组装最终 JSON；调用 done 时 summary 只填字符串 {}，无需抄写字段值。")
        required.add("browser_eval")
        bindings = tuple(require_tool_success("browser_eval", via="codemode", answer_pointer=f"/{key}",
                                             result_pointer=f"/value/{key}") for key in ("amount", "code"))
    session_id = "browser-file-fixture" if upload_file else ""
    if upload_file:
        attachment = InputAttachmentStore(directory / "state", session_id).put(
            "fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode("ascii"))
        task = (f"用户授权浏览器上传已选择的附件 fixture.txt，attachment_id={attachment['id']}。"
                f"用 codemode 的浏览器工具打开 {URL}，snapshot 找到名称为『Attachment』的文件输入框，"
                "browser_upload 选择这个附件，browser_wait 等待页面出现 UPLOAD_BYTES_82，再 browser_read。"
                "用 done 报告实际文件名、文件内容标记和选择回执的 SHA256。不要修改项目或运行命令。")
        required = {"browser_navigate", "browser_snapshot", "browser_upload", "browser_wait", "browser_read"}
    if download_file:
        task = (f"User authorized browser download task. Navigate to {URL}. Use codemode only: browser_snapshot, "
                "then browser_download on the element named Download fixture, then browser_read the current page. "
                "Call done with the actual managed path, filename, byte count and SHA256 from the download receipt. "
                "Do not use run, shell, tests, or guessed values.")
        required = {"browser_navigate", "browser_snapshot", "browser_download", "browser_read"}
    if source_failure:
        task = (f"用生产 open_url 读取必要来源 {URL}。若正式来源回执失败，说明实际限制并调用 done；"
                "不要改用浏览器导航、猜数据或另换来源。")
        required = {"open_url"}
    if fixed_failure:
        task = (f"固定 Chromium 采集程序将先导航至必要来源 {URL} 并读取正文。"
                "请根据真实读取回执解释限制并调用 done，不要猜测正文内容。")
        required = {"browser_navigate", "browser_read"}
    try:
        with tempfile.TemporaryDirectory(prefix="codey-browser-live-project-") as temporary, ExitStack() as stack:
            project = Path(temporary)
            result_file = project / "result.json"
            result_file.write_text('{"amount": 0, "code": "unknown"}\n', encoding="utf-8")
            stack.enter_context(patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}))
            stack.enter_context(patch("codey.app.provider_services.provider_availability", return_value={"local": True}))
            if restore_snapshots:
                stack.enter_context(patch.object(kernel_protocol, "build_turn_snapshot", restored_snapshot))
            if reread:
                stack.enter_context(patch("codey.research.ledger.now_iso", return_value="2026-11-10T00:00:00+00:00"))
            stack.enter_context(patch.object(provider, "_guard_request", route_document))
            stack.enter_context(patch("codey.research.search_factory.default_research_search_provider", source_factory))
            stack.enter_context(patch("codey.operations.task_phases.dispatch.default_research_search_provider", source_factory))
            result = run_headless(HeadlessRequest(
                project if mode == "hybrid" else None, task, intent=mode, provider_id="local", max_turns=12,
                session_id=session_id,
                state_home=directory / "state", research_store_root=directory / "knowledge",
                requested_capabilities=("web.read", "browser.observe", "browser.interact", *(
                    ("browser.effect",) if structured or explanation_output or upload_file or download_file or bound_output else ())),
                project_changes_required=mode == "hybrid", required_tools=(*sorted(required), *bindings),
                output_format="json_explanation" if explanation_output else "json" if bound_output else "text",
                sources_open_required=True if source_failure else None,
                output_schema=json.dumps(SCHEMA) if explanation_output else json.dumps({"type": "object", "properties": {
                    "amount": {"type": "integer"}, "code": {"type": "string"}},
                    "required": ["amount", "code"], "additionalProperties": False}) if bound_output else "",
                model_selection={"model": target.model, "base_url": target.base_url},
                strict_research=strict_research,
            ), emit_jsonl=observe, connect_provider=connect)
            artifact = decode_json_object(json.loads(result_file.read_text(encoding="utf-8")))
            (directory / "project-result.json").write_text(json.dumps(artifact), encoding="utf-8")
        terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
        answer = str(terminal.get("summary") or "")
        successful = {str(row.get("tool_name")) for row in events if row.get("type") == "tool" and row.get("ok") is True}
        reads = sum(row.get("type") == "tool" and row.get("tool_name") == "browser_read" and row.get("ok") is True
                    for row in events)
        failed_edits = sum(row.get("type") == "tool" and row.get("tool_name") == "edit" and row.get("ok") is False
                           for row in events)
        facts_match = ("fixture.txt" in answer and "UPLOAD_BYTES_82" in answer
                       and hashlib.sha256(b"UPLOAD_BYTES_82\n").hexdigest() in answer) if upload_file else (
            "173" in answer and "QUARTZ_851" in answer)
        downloads = list((directory / "state" / "managed_outputs").rglob("*_file_*.bin"))
        if download_file:
            facts_match = (bool(downloads) and all(path.read_bytes() == DOWNLOAD_BYTES for path in downloads)
                           and hashlib.sha256(DOWNLOAD_BYTES).hexdigest() in answer and "fixture-download.txt" in answer)
        output_checks = final_checks(answer, expected_url=URL) if explanation_output else {}
        receipts = tool_receipts(directory / "state")
        failed_refs = sum(not row.result.ok and isinstance(value := row.result.canonical.get("value"), dict)
                          and value.get("reason") == "stale_ref" for row in receipts)
        source_failures = [dict(row.result.canonical) for row in receipts
                           if not row.result.ok and "source_failure" in row.result.canonical]
        passed = (result.stop_reason == "done" and required <= successful and facts_match
                  and (not reread or reads >= 2)
                  and (not stale_edit or changed_after_read and failed_edits >= 1)
                  and (not structured or "SCHEMA_TOKEN_74" in answer and legacy_urls_match(answer))
                  and (not explanation_output or output_checks.get("schema_valid") is True and output_checks.get("exact_fields") is True)
                  and (not stale_ref or failed_refs >= 1)
                  and (mode != "research" or re.search(r"\[e[1-9][0-9]*\]", answer) is not None)
                  and (mode != "hybrid" or artifact == {"amount": 173, "code": "QUARTZ_851"}))
        if source_failure:
            passed = result.stop_reason == "blocked" and bool(source_failures)
        summary: dict[str, object] = {"mode": mode, "passed": passed, "stop_reason": result.stop_reason,
            "successful_tools": sorted(successful), "answer": answer, "project_result": artifact, "navigations": navigations,
            "source_reads": reads, "failed_edits": failed_edits, "changed_after_read": changed_after_read,
            "downloads": [str(path) for path in downloads], "failed_stale_refs": failed_refs,
            "posthoc_output_checks": output_checks, "explanation_output": explanation_output,
            "source_failure_receipts": source_failures}
        explanation = output_checks.get("explanation")
        if isinstance(explanation, str):
            summary["explanation_quality"] = explanation_checks(explanation, 1, expected_url=URL)
            summary["raw_explanation_preserved"] = bool(recording.proposals) and explanation == recording.proposals[-1]["raw_summary"]
    except Exception as exc:
        summary = {"mode": mode, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        provider.close()
        recording.close()
    summary.update(rejection_audit(directory / "state"))
    summary.update(seconds=round(time.monotonic() - started, 3), model=target.model, restored_snapshots=snapshot_count,
                   scope="controlled_fixture_production_entry_and_kernel")
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


class Options(argparse.Namespace):
    run_dir: Path
    mode: str
    timeout: float
    restore_snapshots: bool
    reread: bool
    stale_edit: bool
    structured: bool
    upload_file: bool
    download_file: bool
    bound_output: bool
    kobold_base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("chat", "research", "hybrid", "all"), default="all")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--restore-snapshots", action="store_true", help="Execute serialized/restored production contracts.")
    parser.add_argument("--reread", action="store_true", help="Read changed source twice with a fixed acquisition clock.")
    parser.add_argument("--stale-edit", action="store_true", help="Change the file after its first read; require edit recovery.")
    parser.add_argument("--structured", action="store_true", help="Exercise eval.value and tab_list.tabs declarations.")
    parser.add_argument("--upload-file", action="store_true", help="Upload an explicitly imported task attachment.")
    parser.add_argument("--download-file", action="store_true", help="Download and verify a persistent binary artifact.")
    parser.add_argument("--bound-output", action="store_true", help="Assemble exact JSON fields from committed eval results.")
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Options())
    if args.bound_output and args.mode not in {"chat", "hybrid"}:
        parser.error("--bound-output requires --mode chat or hybrid (Research delivers its submitted Markdown).")
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = select_target(args.timeout, args.kobold_base_url)
    modes = ("chat", "research", "hybrid") if args.mode == "all" else (args.mode,)
    rows = []
    for mode in modes:
        print(json.dumps({"starting": mode}), flush=True)
        row = run_case(root / mode, mode, target, restore_snapshots=args.restore_snapshots, reread=args.reread,
                       stale_edit=args.stale_edit, structured=args.structured, upload_file=args.upload_file,
                       download_file=args.download_file, bound_output=args.bound_output)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = {"passed": sum(row["passed"] is True for row in rows), "runs": len(rows), "results": rows}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if summary["passed"] == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
