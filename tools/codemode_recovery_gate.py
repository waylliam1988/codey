"""Deterministic and KoboldCpp gates for CodeMode receipt-only recovery.

The scripted mode proves the kernel contract with a production task driver.
The KoboldCpp mode uses the same driver and records whether a local model can
follow the ``unwrap`` and ``repair_of`` guidance; it never treats model text as
proof of the side-effect count.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Protocol
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig
from codey.providers.local_connection import connect_local
from codey.research.tools import ResearchTools

_URL = "https://example.test/article"
_TOKEN_RE = re.compile(r"cmr_[A-Za-z0-9_-]{6,64}")


class TextProvider(Protocol):
    native_tools: bool

    def send(self, prompt: str) -> str: ...


class Source:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, _query: str, *, limit: int) -> list[dict[str, object]]:
        return []

    def fetch(self, url: str) -> dict[str, object]:
        self.calls += 1
        return {
            "status": "ok",
            "url": url,
            "title": "Gate article",
            "content_kind": "html",
            "text": f"V{self.calls}\n" * 80,
        }


class ScriptedProvider:
    native_tools = False

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def send(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return json.dumps({
                "tool": "codemode",
                "args": {
                    "code": "const result = await tools.open_url({url: inputs.url}); return result.value.missing;",
                    "inputs": {"url": _URL},
                },
            })
        if len(self.prompts) == 2:
            match = _TOKEN_RE.search(prompt)
            if match is None:
                raise RuntimeError("repair token was not exposed in the recovery prompt")
            return json.dumps({
                "tool": "codemode",
                "args": {
                    "code": "return unwrap(await tools.open_url({url: inputs.url}));",
                    "inputs": {"url": _URL},
                    "repair_of": match.group(0),
                },
            })
        return json.dumps({"tool": "done", "args": {"summary": "Recovered from the committed receipt."}})


def _run(provider: TextProvider, source: Source, directory: Path) -> dict[str, object]:
    from tests.support.kernel_runtime import run_task_kernel

    research_tools = ResearchTools(source, None, None)
    policy = TaskPolicy(frozenset({"control", "project.read", "web.read"}))
    session = TaskSession(
        policy,
        project=str(directory),
        task_text=(
            "This is a CodeMode recovery gate. Use exactly one top-level codemode call. "
            f"The supplied URL is {_URL}. The first codemode program must call open_url for exactly "
            "that URL and intentionally "
            "return a nonexistent property so the kernel issues a repair token. On the next turn, "
            "use that token and repair only the return expression. Then call done."
        ),
        model_tool_names=("codemode", "done"),
        max_turns=4,
    )
    with patch("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: None):
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=provider,
                    run_id="codemode-recovery-gate",
                    effect_scope="task",
                    provider_id="local",
                    user_task=session.task_text,
                ),
                execution=KernelExecutionDeps(
                    project_path=directory,
                    research_tools=research_tools,
                    permission_profile="planning_readonly",
                ),
            ),
        )
    child_rows = [
        row for row in session.executed.values()
        if row.get("name") == "open_url" and ":child:" in str(row.get("call_id", ""))
    ]
    replayed = any(
        result.audit.get("recovery_disposition") == "receipt_replay"
        for result in session._memory_results.values()
        if result.call.name == "codemode"
    )
    return {
        "completed": result.completed,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "source_fetches": source.calls,
        "codemode_open_url_children": len(child_rows),
        "prompt_count": len(getattr(provider, "prompts", ())),
        "executed": list(session.executed.values()),
        "repair_replayed": replayed,
        "ok": result.completed and replayed and source.calls == 1 and len(child_rows) == 1,
    }


def run_scripted() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="codey-codemode-gate-") as raw:
        return _run(ScriptedProvider(), Source(), Path(raw))


def run_koboldcpp(base_url: str, model: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="codey-codemode-kobold-") as raw:
        config = LocalProviderConfig(
            base_url=base_url.rstrip("/"),
            model=model,
            api_key="local",
            native_tools_mode="off",
            api_protocol="openai-completions",
            context=LocalContextBudget(32768, 1024, 12000),
            thinking_enabled=False,
        )
        with closing(connect_local(config=config)) as provider:
            result = _run(provider, Source(), Path(raw))
        result["provider"] = "koboldcpp"
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scripted", "koboldcpp"), default="scripted")
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    if args.mode == "scripted":
        result = run_scripted()
    else:
        if not args.model:
            parser.error("--model is required for --mode koboldcpp")
        result = run_koboldcpp(args.base_url, args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
