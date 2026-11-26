"""Observe live model web actions through unchanged production Chat entry.

Controlled cells replace search/fetch responses only. Live cells use a fresh
headless acquisition browser. No forced reads, scripted model actions or
completion-check changes. Each cell has its own state and raw provider log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from http.client import HTTPException
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.providers.api_provider import ApiProvider
from codey.providers.local_response_codec import normalize_local_reply
from codey.research.browser_search import BrowserSearchProvider
from codey.research.search_contracts import SearchOutcome
from codey.research.web_sources import WebSourceProvider
from tests.manual.ghost_memory_routing.world_model_verification_review import SingleAttemptPolicy, parse_usage
from tests.support.search_fixture import search_outcome
from tools import local_model_gate_attempts as attempts
from tools.shell_web_food_gate import OPEN_TOOLS, opened_bodies

MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"
ENDPOINT = "http://127.0.0.1:5001/v1"


@dataclass(frozen=True)
class Case:
    task: str
    site: str
    city: str
    urls: tuple[str, ...]
    body: str
    anchors: tuple[str, ...]
    failure_urls: tuple[str, ...] = ()
    mixed: bool = False


FOOD_URL = "https://www.zhihu.com/question/816400001/answer/816400002"
TRAVEL_URL = "https://www.xiaohongshu.com/explore/691000000000000000000001"
RECOVERY_URL = "https://www.xiaohongshu.com/explore/691000000000000000000002"
CASES = {
    "zhihu_food": Case("你帮我去知乎上看看福州有什么好吃的", "知乎", "福州", (FOOD_URL,),
        "福州吃食问答。答主：榕城散步者。更新于2026年11月。"
        "这次只推荐两样：芋泥小盏，芋香明显，适合分着吃；花生甜汤，花生炖得软，适合当夜宵。"
        "本回答没有推荐店铺，也未核实任何营业时间。", ("芋泥小盏", "花生甜汤")),
    "xhs_travel": Case("帮我去小红书看看青岛好玩的地方", "小红书", "青岛", (TRAVEL_URL,),
        "青岛游玩笔记。作者：沿坡看海。更新于2026年11月。"
        "我的两站路线：观象山，走上去看看老城区屋顶；团岛山公园，适合慢慢散步。"
        "这条笔记没有介绍崂山，也没有承诺任何开放时间。", ("观象山", "团岛山公园")),
    "unfamiliar_site": Case("去苔页社区查查青岛下雨天适合去哪里，给我两条建议", "苔页社区", "青岛",
        ("https://example.com/taiye-community/notes/qingdao-rain",),
        "苔页社区 · 青岛雨天记录。更新于2026年11月。"
        "路线里推荐青岛市图书馆，适合安静阅读；青岛邮电博物馆，适合看通信历史展陈。"
        "预约和当日开放情况需要另行确认。", ("青岛市图书馆", "青岛邮电博物馆")),
    "mixed_sources": Case("知乎里福州甜食有什么推荐？帮我去看看，再说说推荐理由。", "知乎", "福州", (FOOD_URL,),
        "知乎：福州甜食体验。答主：河边小碗。"
        "我推荐荔枝冻，清凉而且有果香；橄榄蜜饯，酸甜适合少量尝。"
        "这份回答没有讨论安泰社区食堂，也没有提供店铺排名。", ("荔枝冻", "橄榄蜜饯"), mixed=True),
    "recover_open": Case("到小红书查查青岛有什么值得逛的地方，给我两个选择。", "小红书", "青岛",
        (TRAVEL_URL, RECOVERY_URL),
        "青岛街区漫步笔记。作者：坡路慢游。"
        "两个选择：银鱼巷，有旧街区与小店；浮山森林公园，可走山间步道。"
        "路线不包含海水浴场，天气不适合时可改日。", ("银鱼巷", "浮山森林公园"), (TRAVEL_URL,)),
    "all_blocked": Case("帮我去知乎看看福州的小吃推荐，告诉我你查到了什么。", "知乎", "福州",
        (FOOD_URL, "https://www.zhihu.com/question/816400003/answer/816400004"), "", (),
        (FOOD_URL, "https://www.zhihu.com/question/816400003/answer/816400004")),
}
CONTROLS = {
    "explicit_food": ("zhihu_food", "请先搜索知乎的福州美食相关内容，再打开一条知乎回答阅读正文，"
        "最后依据正文给我两样吃食和推荐理由；打不开就如实说明。"),
    "explicit_recovery": ("recover_open", "请先搜索小红书的青岛游玩笔记，再打开结果阅读正文。"
        "如果某篇打不开，可尝试其他相关结果；最后依据正文推荐两个地方，无法读取就如实说明。"),
}


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def backend_status() -> dict[str, object]:
    with urlopen("http://127.0.0.1:5001/api/extra/perf", timeout=5) as response:
        return json.load(response)


def owned_generation(payload: dict[str, object]) -> None:
    # KoboldCpp preserves this transport-only key; it is not model prompt text.
    payload["genkey"] = "codey-web-action-" + uuid.uuid4().hex


class ProbeProvider(attempts.RecordingProvider):
    def __init__(self, directory: Path, native: bool) -> None:
        ApiProvider.__init__(self, ENDPOINT, MODEL, temperature=0, timeout=120, output_tokens=8192,
            context_window_tokens=262144, context_reserve_tokens=32768, context_keep_recent_tokens=32000,
            native_tools=native, text_decoder=normalize_local_reply, usage_parser=parse_usage,
            request_policy=SingleAttemptPolicy(), configure_request=owned_generation)
        self.directory = directory
        self.exchange_number = 0
        self.recorder_id = uuid.uuid4().hex


@contextmanager
def controlled(case: Case, directory: Path) -> Iterator[None]:
    def search(_self: WebSourceProvider, query: str, *, limit: int = 8) -> SearchOutcome:
        hits: list[dict[str, object]] = []
        if case.city in query or ("fuzhou" if case.city == "福州" else "qingdao") in query.lower():
            if case.mixed:
                hits.append({"title": "福州美食榜单", "url": "https://www.toutiao.com/article/816400005/",
                    "snippet": "安泰社区食堂和佛跳墙都不错，网友多次推荐。"})
            for index, url in enumerate(case.urls):
                hits.append({"title": f"{case.site}：{case.city}体验记录 {index + 1}", "url": url,
                    "snippet": "作者记录了当地体验，正文含具体选择与理由。"})
        append(directory / "acquisitions.jsonl", {"kind": "search", "query": query, "results": hits[:limit]})
        return search_outcome(query, hits[:limit], limit=limit)

    def fetch(_self: WebSourceProvider, url: str) -> dict[str, object]:
        if url in case.failure_urls:
            page: dict[str, object] = {"status": "error", "url": url,
                "detail": "访问受限：需要登录，未取得回答或笔记正文。", "failure_code": "login_required"}
        elif url in case.urls:
            page = {"status": "ok", "url": url, "title": f"{case.site} {case.city}体验记录",
                "text": case.body, "content_kind": "html"}
        elif "toutiao.com" in url and case.mixed:
            page = {"status": "ok", "url": url, "title": "头条：福州美食榜单",
                "text": "这是头条文章，不是知乎回答。推荐佛跳墙与安泰社区食堂。", "content_kind": "html"}
        else:
            page = {"status": "error", "url": url, "detail": "此地址没有可读取的来源。", "failure_code": "page_not_loaded"}
        append(directory / "acquisitions.jsonl", {"kind": "fetch", "url": url, "page": page})
        return page

    with patch.object(WebSourceProvider, "search", search), patch.object(WebSourceProvider, "fetch", fetch):
        yield


def records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def analyze(directory: Path, case: Case, live: bool) -> dict[str, object]:
    events = records(directory / "events.jsonl")
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
    calls = [row for row in events if row.get("type") == "tool"]
    bodies = opened_bodies(directory) if (directory / "provider.jsonl").exists() else []
    acquisitions = records(directory / "acquisitions.jsonl")
    reads = [row for row in acquisitions if row.get("kind") == "fetch"]
    successful = [row for row in reads if isinstance(row.get("page"), dict) and row["page"].get("status") == "ok"]
    target = [row for row in successful if row.get("url") in case.urls]
    searches = [row for row in calls if row.get("tool_name") == "web_search"]
    opens = [row for row in calls if row.get("tool_name") in OPEN_TOOLS]
    summary = str(terminal.get("summary") or "")
    answers = {term: term in summary for term in case.anchors}
    first_target = next((i for i, row in enumerate(reads) if row in target), None)
    recovered = bool(first_target is not None and any(row.get("url") in case.failure_urls for row in reads[:first_target]))
    requests = [row for row in records(directory / "provider.jsonl") if row.get("type") == "request"]
    responses = [row for row in records(directory / "provider.jsonl") if row.get("type") == "response"]
    return {"stop_reason": terminal.get("stop_reason"), "summary": summary, "tools": calls,
        "search_count": len(searches), "open_attempt_count": len(opens), "delivered_open_body_count": len(bodies),
        "target_body_acquired": bool(target) if not live else None,
        "body_only_terms_in_answer": answers if not live else None,
        "observed_failure_then_target_success": recovered if not live else None,
        "action_chain_pass": (bool(searches and target and bodies) if not live and case.body else None),
        "observation_adoption_pass": (bool(target and bodies and all(answers.values())) if not live and case.anchors else None),
        "failure_read_attempted": any(row.get("url") in case.failure_urls for row in reads),
        "request_count": len(requests), "response_count": len(responses),
        "usage": [row.get("payload", {}).get("usage") for row in responses],
        "manual_review_required": True}


def run_cell(root: Path, name: str, protocol: str, live: bool) -> None:
    before = backend_status()
    if not before.get("idle") or before.get("queue"):
        append(root / "scheduler-halts.jsonl", {"case": name, "protocol": protocol, "live": live, "backend": before})
        raise RuntimeError("Backend still busy; do not queue another cell")
    source, task = CONTROLS[name] if name in CONTROLS else (name, CASES[name].task)
    case = CASES[source]
    directory = root / f"{'live' if live else 'controlled'}-{name}-{protocol}"
    directory.mkdir(exist_ok=False)
    write(directory / "backend-before.json", before)
    native = protocol == "native"
    write(directory / "manifest.json", {"case": name, "source_case": source, "task": task,
        "protocol": protocol, "live": live, "model": MODEL, "max_turns": 8,
        "source_fixture": None if live else asdict(case), "forced_source_requirement": False})
    target = attempts.GateTarget(ENDPOINT, MODEL, 262144, 32768, 32000, protocol=protocol)

    def connect(provider_id: str, **_kwargs: object) -> ProbeProvider:
        if provider_id != "local":
            raise ValueError("Only the pinned local model is admitted")
        return ProbeProvider(directory, native)

    def observe(row: dict[str, object]) -> None:
        append(directory / "events.jsonl", row)

    original_init = WebSourceProvider.__init__

    def isolated_browser(self: WebSourceProvider, **kwargs: object) -> None:
        kwargs["browser_factory"] = lambda: BrowserSearchProvider(headless=True, isolated=True)
        original_init(self, **kwargs)

    started = time.monotonic()
    try:
        with (
            patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1" if native else "0"}),
            attempts.isolate_gate_secondary_models(target),
            patch.object(WebSourceProvider, "__init__", isolated_browser) if live else controlled(case, directory),
        ):
            run_headless(HeadlessRequest(None, task, intent="chat", provider_id="local", max_turns=8,
                state_home=directory / "state", requested_capabilities=("web.read",),
                sources_open_required=False, project_changes_required=False,
                model_selection={"model": MODEL, "base_url": ENDPOINT}),
                emit_jsonl=observe, connect_provider=connect)
        report = analyze(directory, case, live)
    except Exception as exc:
        report = {"exception": type(exc).__name__, "detail": str(exc), **analyze(directory, case, live)}
    report.update({"cell": directory.name, "seconds": round(time.monotonic() - started, 3)})
    write(directory / "summary.json", report)
    try:
        after = backend_status()
        if not after.get("idle"):
            requests = [row for row in records(directory / "provider.jsonl") if row.get("type") == "request"]
            key = requests[-1].get("payload", {}).get("genkey") if requests else None
            if isinstance(key, str) and key.startswith("codey-web-action-"):
                request = Request("http://127.0.0.1:5001/api/extra/abort", data=json.dumps({"genkey": key}).encode(),
                    headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=5) as response:
                    report["owned_abort"] = json.load(response)
        report["backend_after"] = after
    except (OSError, ValueError, HTTPException) as exc:
        report["backend_telemetry_error"] = f"{type(exc).__name__}: {exc}"
    write(directory / "summary.json", report)
    print(json.dumps({key: report.get(key) for key in ("cell", "stop_reason", "action_chain_pass",
        "observation_adoption_pass", "search_count", "open_attempt_count", "request_count", "exception", "seconds")}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--case", choices=(*CASES, *CONTROLS))
    parser.add_argument("--protocol", choices=("native", "text"), default="native")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        args.root.mkdir(parents=True, exist_ok=False)
        preflight = {}
        for route in ("/v1/models", "/api/extra/version", "/api/extra/perf"):
            with urlopen("http://127.0.0.1:5001" + route, timeout=5) as response:
                preflight[route] = json.load(response)
        if preflight["/v1/models"]["data"][0]["id"] != MODEL:
            raise ValueError("Loaded model changed")
        write(args.root / "preflight.json", preflight)
        write(args.root / "protocol.json", {"model": MODEL, "endpoint": ENDPOINT, "temperature": 0,
            "output_tokens": 8192, "stream": False, "context": 262144, "reserve": 32768, "keep": 32000,
            "max_turns": 8, "request_timeout_seconds": 120, "physical_attempts": 1,
            "cases": {name: asdict(case) for name, case in CASES.items()}, "controls": CONTROLS,
            "matrix": [{"case": name, "protocol": protocol, "live": False}
                       for name in (*CASES, *CONTROLS) for protocol in ("native", "text")]
                      + [{"case": name, "protocol": "native", "live": True} for name in ("zhihu_food", "xhs_travel")],
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "limits": "18 independent state cells, 8 scenario prompts; no repeated stochastic samples. Controlled sources are synthetic, not live site visits. No completion gates changed."})
        print(str(args.root), flush=True)
    elif args.case:
        run_cell(args.root, args.case, args.protocol, args.live)
    else:
        parser.error("Use --prepare or --case")


if __name__ == "__main__":
    main()
