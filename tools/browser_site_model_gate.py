"""Paired real-site research through KoboldCpp and the existing production kernel."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.automation.browser import CdpEndpoint
from codey.env_names import NATIVE_TOOLS_ENV
from codey.research.chat_web import ChatWebProvider
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object
from tools import local_model_gate_attempts as attempts
from tools.browser_experiment_sources import ResumableBrowserSearchProvider
from tools.browser_semantic_live_gate import delivered_originals, model_usage
from tools.browser_site_comparison import SITES
from tools.browser_user_session_gate import targets
from tools.shell_web_diagnostic_gate import select_target


def run(directory: Path, timeout: float, repeats: int, *, user_cdp_port: int = 0,
        selected_sites: tuple[str, ...] = ()) -> None:
    sites = dict(SITES)
    available_tasks = (
        ("toutiao", "列出文章实际介绍的三种福州小吃，并概述各自特点，带原文引用。"),
        ("sohu", "根据文章列出三个榜单小吃及其特点，保留来源说法，不自行补充。"),
        ("python_docs", "根据这份官方文档解释 TaskGroup 如何处理子任务非取消异常、其他子任务和异常组，保留例外。"),
        ("pdf", "引用这份 PDF 的原文文字，并说明实际读到了什么。"),
        ("zhihu_old", "列出三种文中实际介绍的福州小吃及特点，保留来源说法，并引用实际原文。"),
        ("zhihu_new", "列出三种文中实际介绍的福州小吃及特点，保留来源说法，并引用实际原文。"),
    )
    tasks = tuple(row for row in available_tasks if row[0] in selected_sites) if selected_sites else available_tasks[:4]
    rows: list[JsonObject] = []
    target = select_target("gemma", timeout, "http://127.0.0.1:5001/v1")
    manifest = canonical_json_object({"git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "repeats": repeats, "timeout": timeout, "model": target.model,
                "temperature": target.temperature, "count": len(tasks) * repeats * 2,
                "user_cdp_port": user_cdp_port, "selected_sites": list(selected_sites),
                "scope": "real_sites_real_model_production_kernel",
                "session": "existing_user_cdp" if user_cdp_port else "fresh_isolated_headless"})
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("site experiment inputs changed; preserve the run and use a new directory")
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for repeat in range(1, repeats + 1):
        for name, question in tasks:
            for candidate in ([False, True] if repeat % 2 else [True, False]):
                arm = "browser_first" if candidate else "http_first"
                case_dir = directory / f"{name}-{repeat}-{arm}"
                if (case_dir / "summary.json").is_file():
                    saved = decode_json_object(json.loads((case_dir / "summary.json").read_text(encoding="utf-8")))
                    rows.append(saved)
                    continue
                case_dir.mkdir()
                browser = ResumableBrowserSearchProvider(headless=not bool(user_cdp_port), isolated=False,
                                                          cdp_port=user_cdp_port or 9262, http_fallback=False)

                def endpoint(**kwargs: object) -> CdpEndpoint:
                    return CdpEndpoint(user_cdp_port, None)

                def browser_factory(selected: ResumableBrowserSearchProvider = browser) -> ResumableBrowserSearchProvider:
                    return selected

                source = browser if candidate else ChatWebProvider(browser_factory=browser_factory)
                events: list[JsonObject] = []

                def observe(row: dict[str, object], *, sink: list[JsonObject] = events,
                            destination: Path = case_dir) -> None:
                    sink.append(canonical_json_object(row))
                    with (destination / "events.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")

                def connect(provider_id: str, destination: Path = case_dir, **kwargs: object) -> attempts.RecordingProvider:
                    if provider_id != "local":
                        raise ValueError("real-site gate requires the selected local model")
                    return attempts.RecordingProvider(target, destination)

                def source_factory(selected: ChatWebProvider | ResumableBrowserSearchProvider = source
                                   ) -> ChatWebProvider | ResumableBrowserSearchProvider:
                    return selected

                started = time.monotonic()
                task = f"研究此资料：{sites[name]}。{question} 仅用此来源；不能读取时如实说明。不要保存知识。"
                try:
                    with ExitStack() as stack:
                        if user_cdp_port:
                            targets(user_cdp_port)
                            stack.enter_context(patch("codey.automation.browser._ensure_cdp_endpoint", endpoint))
                        stack.enter_context(patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}))
                        stack.enter_context(attempts.isolate_gate_secondary_models(target))
                        stack.enter_context(attempts.record_api_generations(case_dir / "provider.jsonl"))
                        stack.enter_context(patch("codey.research.search_factory.default_research_search_provider", source_factory))
                        stack.enter_context(patch("codey.operations.task_phases.dispatch.default_research_search_provider", source_factory))
                        result = run_headless(HeadlessRequest(None, task, intent="research", provider_id="local", max_turns=10,
                            state_home=case_dir / "state", research_store_root=case_dir / "knowledge",
                            model_selection={"model": target.model, "base_url": target.base_url},
                            project_changes_required=False), emit_jsonl=observe, connect_provider=connect)
                    requests = [decode_json_object(json.loads(line)) for line in
                                (case_dir / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
                    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), None)
                    answer = str(terminal.get("summary") or "") if terminal is not None else ""
                    report = canonical_json_object({"site": name, "repeat": repeat, "arm": arm, "task": task,
                        "stop_reason": result.stop_reason, "answer": answer, "original_passages": delivered_originals(requests),
                        "seconds": round(time.monotonic() - started, 3), "usage": model_usage(requests),
                        "tools": [row for row in events if row.get("type") == "tool"],
                        "limits": "Live websites in the recorded session. Manual review required; no automatic quality certification."})
                finally:
                    source.close()
                (case_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                rows.append(report)
                (directory / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({key: report[key] for key in ("site", "repeat", "arm", "stop_reason", "seconds")}), flush=True)
    (directory / "summary.json").write_text(json.dumps({**manifest, "rows": rows, "completed": len(rows)},
                                                       ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--user-cdp-port", type=int, default=0)
    parser.add_argument("--sites", nargs="+", choices=[name for name, _ in SITES], default=[])
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    run(directory, args.timeout, args.repeats, user_cdp_port=args.user_cdp_port, selected_sites=tuple(args.sites))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
