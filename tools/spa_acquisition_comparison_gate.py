"""Same original prompts/model/source fixtures through selected production checkouts.

Only HTTP/search/browser network responses are fixtures. The model, production
headless entry, JavaScript rendering, receipts and completion remain real.
This records answers for review; it does not certify natural-language accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

SITE = "https://api.lihe.chat/"
FOOD = "https://example.com/fuzhou-food-record"
TASKS = {"site_spa": SITE + "  你帮我看看这个网站是干嘛的",
         "site_static": SITE + "  你帮我看看这个网站是干嘛的",
         "site_blank": SITE + "  你帮我看看这个网站是干嘛的",
         "food": "你帮我上网看看福州有什么好吃的"}
SITE_BODY = ("ToCreate。一个密钥，调用多个模型。登录后购买套餐，即可用同一个密钥调用 Claude、GPT、Gemini。"
             "一个密钥对接常用客户端，按套餐购买额度。支持设置配额上限。")
FOOD_BODY = ("福州小吃实地记录：鱼丸用鱼肉制成丸皮，包入肉馅，适合喜欢汤食的人。"
             "肉燕以肉制燕皮包馅，口感轻薄。锅边糊把米浆沿锅边烫熟，配汤食用，适合作早餐。"
             "本页只记录这三种小吃，没有列出餐馆、地址、价格或排名。")


class FixtureResponse(io.BytesIO):
    status = 200
    headers = {"content-type": "text/html; charset=utf-8"}

    def __init__(self, url: str, html: str) -> None:
        super().__init__(html.encode())
        self.url = url

    def geturl(self) -> str:
        return self.url


def run(repo: Path, directory: Path, case: str, timeout: float, *, acquisition_only: bool = False) -> None:
    # The external script is identical for old and new checkouts. Import only
    # after selecting the checkout, including its production provider helpers.
    sys.path.insert(0, str(repo))
    from playwright.sync_api import Route

    from codey.app.headless_runner import HeadlessRequest, run_headless
    from codey.env_names import NATIVE_TOOLS_ENV
    from codey.research.web_sources import WebSourceProvider
    from codey.types.json_value import JsonObject, decode_json_object
    from tools import local_model_gate_attempts as attempts
    from tools.shell_web_diagnostic_gate import select_target
    from tools.shell_web_food_gate import opened_bodies

    html = "<html><title>Unavailable</title><body></body></html>"
    if case == "site_spa":
        html = ('<html><title>ToCreate</title><body><div id="root"></div><script type="module">'
                "document.querySelector('#root').innerText=" + json.dumps(SITE_BODY) + ";</script></body></html>")
    elif case == "site_static":
        html = "<html><title>ToCreate</title><body>" + SITE_BODY + "</body></html>"
    elif case == "food":
        html = "<html><title>福州小吃记录</title><body><article>" + FOOD_BODY + "</article></body></html>"
    (directory / "page.html").write_text(html, encoding="utf-8")

    def http(request: Request, *, timeout: float) -> FixtureResponse:
        return FixtureResponse(request.full_url, html)

    def browser_response(self: object, route: Route) -> None:
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    if acquisition_only:
        from dataclasses import asdict

        from codey.research.ledger import ResearchLedger
        from codey.research.source_gateway import ResearchSourceGateway

        provider = WebSourceProvider()
        url = FOOD if case == "food" else SITE
        try:
            with (patch("codey.research.source_download._open_url_no_redirect", http),
                  patch("codey.research.browser_search.BrowserSearchProvider._guard_request", browser_response)):
                outcome = ResearchSourceGateway(provider, ResearchLedger(), direct_urls=frozenset({url})).open(url)
            result = asdict(outcome)
            (directory / "acquisition.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=True), flush=True)
        finally:
            provider.close()
        return

    target = select_target("gemma", timeout, "http://127.0.0.1:5001/v1")
    manifest = {"scope": "fixed_sources_real_model_production_entry", "case": case,
                "task": TASKS[case], "repo": str(repo), "model": target.model,
                "temperature": target.temperature, "max_turns": 8,
                "request_timeout_seconds": timeout,
                "fixture_sha256": hashlib.sha256(html.encode()).hexdigest(),
                "git_head": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
                "limits": "One trial; source responses are controlled; answer accuracy requires review."}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def search(self: WebSourceProvider, query: str, *, limit: int = 8) -> list[JsonObject]:
        return [{"title": "福州小吃记录" if case == "food" else "Site fixture",
                 "url": FOOD if case == "food" else SITE, "snippet": "Open the page for its actual contents."}]

    def observe(row: dict[str, object]) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("comparison uses only the selected local model")
        return attempts.RecordingProvider(target, directory)

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}))
        stack.enter_context(attempts.isolate_gate_secondary_models(target))
        stack.enter_context(attempts.record_api_generations(directory / "provider.jsonl"))
        stack.enter_context(patch("codey.research.source_download._open_url_no_redirect", http))
        stack.enter_context(patch.object(WebSourceProvider, "search", search))
        stack.enter_context(patch("codey.research.browser_search.BrowserSearchProvider._guard_request", browser_response))
        run_headless(HeadlessRequest(None, TASKS[case], intent="chat", provider_id="local", max_turns=8,
            state_home=directory / "state", model_selection={"model": target.model, "base_url": target.base_url},
            requested_capabilities=(), project_changes_required=False), emit_jsonl=observe, connect_provider=connect)
    events = [decode_json_object(json.loads(line)) for line in
              (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
    report = {**manifest, "stop_reason": terminal.get("stop_reason"), "summary": terminal.get("summary"),
              "tools": [row for row in events if row.get("type") == "tool"],
              "delivered_page_bodies": opened_bodies(directory) if (directory / "provider.jsonl").is_file() else [],
              "answer_review_required": True}
    (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"case": case, "stop_reason": report["stop_reason"], "summary": report["summary"]}, ensure_ascii=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--case", choices=tuple(TASKS), required=True)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--acquisition-only", action="store_true")
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    run(args.repo.resolve(), directory, args.case, args.timeout, acquisition_only=args.acquisition_only)


if __name__ == "__main__":
    main()
