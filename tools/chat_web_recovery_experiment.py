"""Paired local-model trials using production Chat, with opt-in experiment patches.

Controlled mode fixes only source responses. Live mode uses the default real
browser. Candidate policies are never installed outside each worker process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Literal
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.research.web_sources import WebSourceProvider
from codey.types.json_value import JsonObject, decode_json_object
from tools import local_model_gate_attempts as attempts
from tools.chat_web_experiment_candidates import INTERNAL_REFS, VARIANTS, Variant, candidate_variant
from tools.shell_web_diagnostic_gate import select_target
from tools.shell_web_food_gate import OPEN_TOOLS, opened_bodies

Mode = Literal["controlled", "live"]


@dataclass(frozen=True)
class Case:
    task: str
    entity: str
    title: str
    body: str
    expected: tuple[str, ...]
    recovery: bool = False
    body_required: bool = True
    source_url: str = ""

    @property
    def url(self) -> str:
        return self.source_url or "https://example.com/" + hashlib.sha256(self.title.encode()).hexdigest()[:12]


def _long_body(topic: str, findings: str) -> str:
    sections = [f"## 场所与交通 {n}\n" + "本节说明交通接驳、街区布局、路线及公共空间。" * 30 for n in range(12)]
    return "\n\n".join((*sections, f"## {topic}\n{findings}"))


FOOD = "鱼丸用鱼肉制成丸皮，包入肉馅。肉燕以肉制燕皮包馅。锅边糊用米浆沿锅边烫熟，适合作早餐。"
CASES: dict[str, Case] = {
    "food_recovery": Case("你帮我上网看看福州有什么好吃的", "福州美食", "福州小吃记录", FOOD,
                          ("鱼丸", "肉燕", "锅边糊"), recovery=True),
    "nearby_recovery": Case("东百中心 ？你帮我搜索一下，这里附近有什么好玩的", "东百中心", "东百中心周边记录",
                            "东百中心位于福州东街口商圈。三坊七巷适合逛历史街巷。达明美食街适合吃小吃。乌山适合散步。",
                            ("三坊七巷", "达明美食街", "乌山"), recovery=True),
    "chengdu_recovery": Case("帮我上网看看成都太古里附近有什么值得逛的", "成都太古里", "成都太古里周边记录",
                             "成都太古里附近有大慈寺，可看古寺院落；春熙路适合逛街；镋钯街有街边小店。",
                             ("大慈寺", "春熙路", "镋钯街"), recovery=True),
    "food_reading": Case("帮我看看这个网页介绍了哪些福州美食：{url}", "福州美食", "福州城市长文",
                         _long_body("美食推荐", FOOD), ("鱼丸", "肉燕", "锅边糊")),
    "security_reading": Case("帮我看看这个网页对账号安全有什么建议：{url}", "账号安全", "服务使用指南",
                             _long_body("账号安全", "启用通行密钥。离线保存恢复码。撤销不再使用的会话。"),
                             ("通行密钥", "恢复码", "会话")),
    "food_citations": Case("你帮我上网看看福州有什么好吃的", "福州美食", "福州小吃记录", FOOD,
                           ("鱼丸", "肉燕", "锅边糊")),
    "link_only": Case("帮我找一下成都太古里的官网链接", "成都太古里", "成都太古里官方网站", "商场官方网站。", (),
                      body_required=False),
    "normal_chat": Case("你好，简单介绍一下自己", "", "", "", (), body_required=False),
    "travel_reading": Case("帮我看看这个网页介绍了哪些福州美食：{url}", "福州美食", "福州城市长文",
                           _long_body("美食推荐", FOOD), ("鱼丸", "肉燕", "锅边糊"),
                           source_url="https://chinatraveljetso.com/%e7%a6%8f%e5%b7%9e%e8%87%aa%e7%94%b1%e8%a1%8c/"),
    "table_fields": Case("请查看这个网页的“必試地道美食”表格，分别整理美食名称、特色和推荐店家：{url}",
                         "福州美食", "福州城市长文",
                         _long_body("必試地道美食", "南煎肝\n\n豬肝嫩滑，醬汁香濃\n\n聚春園\n\n鍋邊\n\n米漿攤成薄片\n\n老福洲"),
                         ("南煎肝", "鍋邊"),
                         source_url="https://chinatraveljetso.com/%e7%a6%8f%e5%b7%9e%e8%87%aa%e7%94%b1%e8%a1%8c/"),
}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def fixed_sources(case: Case, directory: Path) -> Iterator[None]:
    """A toy search service: verbose queries miss the entity; core queries find it.

    The query rule is a declared network fixture, never production rewriting.
    Raw source data has no instruction to open, continue or finish the task.
    """
    def search(provider: WebSourceProvider, query: str, *, limit: int = 8) -> list[JsonObject]:
        compact = "".join(query.split())
        core = case.entity in compact and len(compact) <= len(case.entity) + 2
        results: list[JsonObject]
        if case.recovery and not core:
            results = [{"title": "东（汉语文字）", "url": "https://example.com/dictionary",
                        "snippet": "东是汉字，表示方向，与西相对。"}]
        else:
            results = [{"title": case.title, "url": case.url, "snippet": "街区资讯、交通与主题介绍。"}]
        with (directory / "search-responses.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"query": query, "results": results}, ensure_ascii=False) + "\n")
        return results[:limit]

    def fetch(url: str) -> dict[str, object]:
        if url == case.url:
            return {"status": "ok", "url": url, "title": case.title, "text": case.body, "content_kind": "html"}
        return {"status": "error", "url": url, "error": "Fixture source unavailable"}

    with (patch.object(WebSourceProvider, "search", search),
          patch("codey.research.web_sources.fetch_http_document", fetch)):
        yield


def provider_usage(directory: Path) -> JsonObject:
    """The two existing observers see the same response; count each id once."""
    seen: set[str] = set()
    prompt_tokens = completion_tokens = 0
    for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines():
        row = decode_json_object(json.loads(line))
        payload = row.get("payload")
        if row.get("type") != "response" or not isinstance(payload, dict):
            continue
        identity, usage = payload.get("id"), payload.get("usage")
        if not isinstance(identity, str) or identity in seen or not isinstance(usage, dict):
            continue
        seen.add(identity)
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if type(prompt) is int and type(completion) is int:
            prompt_tokens += prompt
            completion_tokens += completion
    return {"generations": len(seen), "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}


def analyze(directory: Path, case: Case, mode: Mode) -> JsonObject:
    events = [decode_json_object(json.loads(line)) for line in
              (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
    tools = [row for row in events if row.get("type") == "tool"]
    searches = [row for row in tools if row.get("tool_name") == "web_search"]
    opens = [row for row in tools if row.get("tool_name") in OPEN_TOOLS and row.get("ok") is True]
    bodies = opened_bodies(directory)
    # Exclude the bounded heading outline from actual body evidence.
    evidence = "\n".join(body.split("Source section locators (labels only):", 1)[0] for body in bodies)
    summary = str(terminal.get("summary", ""))
    internal = bool(INTERNAL_REFS.search(summary))
    relevant_body = bool(bodies) and all(term in evidence for term in case.expected)
    expected_answer = all(term in summary for term in case.expected)
    source_link = case.url in summary if mode == "controlled" else "http" in summary
    if case.body_required:
        structural_ok = terminal.get("stop_reason") == "done" and bool(opens and bodies) and source_link and not internal
    else:
        structural_ok = terminal.get("stop_reason") == "done" and not internal
        structural_ok = structural_ok and (source_link if case.entity else not tools)
    report: JsonObject = {"stop_reason": terminal.get("stop_reason"), "summary": summary,
        "queries": [row.get("path") for row in searches], "search_count": len(searches), "open_count": len(opens),
        "tool_count": len(tools), "turns": terminal.get("turns"), "delivered_body_count": len(bodies),
        "source_link": source_link, "internal_references": internal, "structural_ok": structural_ok,
        "controlled_expected_terms_in_body": relevant_body if mode == "controlled" else None,
        "controlled_expected_terms_in_answer": expected_answer if mode == "controlled" else None,
        "controlled_evidence_ok": structural_ok and (not case.body_required or relevant_body and expected_answer)
        if mode == "controlled" else None, "usage": provider_usage(directory), "answer_review_required": True,
        "cross_query_feedback_delivered": "The web_search query changed, but its returned output is identical"
        in (directory / "provider.jsonl").read_text(encoding="utf-8")}
    return report


def aggregate(trials: list[JsonObject]) -> JsonObject:
    groups: dict[tuple[str, str], list[JsonObject]] = {}
    for row in trials:
        groups.setdefault((str(row["case"]), str(row["variant"])), []).append(row)
    rows: list[JsonObject] = []
    for (case, variant), observations in sorted(groups.items()):
        tokens: list[int] = []
        turns: list[int] = []
        for row in observations:
            count = row.get("turns")
            if type(count) is int:
                turns.append(count)
            usage = row.get("usage")
            if isinstance(usage, dict):
                count = usage.get("prompt_tokens")
                if type(count) is int:
                    tokens.append(count)
        rows.append({"case": case, "variant": variant, "trials": len(observations),
            "structural_passes": sum(row.get("structural_ok") is True for row in observations),
            "controlled_evidence_passes": sum(row.get("controlled_evidence_ok") is True for row in observations),
            "relevant_body_deliveries": sum(row.get("controlled_expected_terms_in_body") is True for row in observations),
            "internal_reference_failures": sum(row.get("internal_references") is True for row in observations),
            "median_turns": median(turns) if turns else None,
            "median_prompt_tokens": median(tokens) if tokens else None})
    return {"groups": [row for row in rows], "manual_answer_review_required": True}


def run_trial(directory: Path, name: str, variant: Variant, mode: Mode, timeout: float) -> JsonObject:
    from codey.env_names import NATIVE_TOOLS_ENV

    case = CASES[name]
    task = case.task.format(url=case.url)
    target = select_target("gemma", timeout, "http://127.0.0.1:5001/v1")
    manifest: JsonObject = {"case": name, "variant": variant, "mode": mode, "task": task, "model": target.model,
        "temperature": target.temperature, "max_turns": 8, "timeout": timeout,
        "source_sha256": hashlib.sha256(case.body.encode()).hexdigest() if mode == "controlled" else None,
        "candidate_sha256": hashlib.sha256(Path(__file__).with_name("chat_web_experiment_candidates.py").read_bytes()).hexdigest(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    write_json(directory / "manifest.json", manifest)

    def observe(row: dict[str, object]) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("experiment uses only the selected local model")
        return attempts.RecordingProvider(target, directory)

    started = time.monotonic()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}))
        stack.enter_context(attempts.isolate_gate_secondary_models(target))
        stack.enter_context(attempts.record_api_generations(directory / "provider.jsonl"))
        stack.enter_context(candidate_variant(variant))
        if mode == "controlled":
            stack.enter_context(fixed_sources(case, directory))
        run_headless(HeadlessRequest(None, task, intent="chat", provider_id="local", max_turns=8,
            state_home=directory / "state", model_selection={"model": target.model, "base_url": target.base_url},
            requested_capabilities=(), project_changes_required=False), emit_jsonl=observe, connect_provider=connect)
    report = {**manifest, **analyze(directory, case, mode), "seconds": round(time.monotonic() - started, 3)}
    write_json(directory / "summary.json", report)
    return report


class Arguments(argparse.Namespace):
    run_dir: Path
    cases: list[str]
    variants: list[str]
    repeats: int
    mode: Mode
    timeout: float
    worker: bool


def parse_variant(value: str) -> Variant:
    for variant in VARIANTS:
        if value == variant:
            return variant
    raise ValueError(f"unknown variant: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES), required=True)
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=["baseline", "combined"])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--mode", choices=("controlled", "live"), default="controlled")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args(namespace=Arguments())
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    if args.worker:
        report = run_trial(directory, args.cases[0], parse_variant(args.variants[0]), args.mode, args.timeout)
        print(json.dumps({key: report[key] for key in ("case", "variant", "stop_reason", "structural_ok",
            "controlled_evidence_ok", "seconds")}), flush=True)
        return 0
    plan = {"cases": args.cases, "variants": args.variants, "repeats": args.repeats, "mode": args.mode,
            "promotion_rule": "No production promotion from tool success alone. Require paired repeated evidence gains, "
            "manual answer review, no normal-chat/link-only regression, and no material turn/token regression.",
            "limits": "Controlled fixtures do not certify search engines; live trials need manual relevance review."}
    write_json(directory / "plan.json", plan)
    reports: list[JsonObject] = []
    for repeat in range(args.repeats):
        order = args.variants if repeat % 2 == 0 else list(reversed(args.variants))
        for case in args.cases:
            for variant in order:
                trial_dir = directory / f"r{repeat + 1}-{case}-{variant}"
                command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--run-dir", str(trial_dir),
                    "--cases", case, "--variants", variant, "--mode", args.mode, "--timeout", str(args.timeout)]
                try:
                    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                               errors="replace", timeout=max(180, args.timeout * 2))
                    (directory / f"{trial_dir.name}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
                    if completed.returncode != 0:
                        raise RuntimeError(f"worker exited {completed.returncode}; see {trial_dir.name}.log")
                    report = decode_json_object(json.loads((trial_dir / "summary.json").read_text(encoding="utf-8")))
                except (subprocess.TimeoutExpired, RuntimeError) as exc:
                    report = {"case": case, "variant": variant, "error": str(exc), "structural_ok": False}
                reports.append(report)
                write_json(directory / "summary.json", {"plan": plan, "trials": [row for row in reports],
                    "aggregate": aggregate(reports)})
                print(json.dumps({key: report.get(key) for key in ("case", "variant", "stop_reason",
                    "controlled_evidence_ok", "structural_ok", "seconds", "error")}), flush=True)
    return 0 if all("error" not in row for row in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
