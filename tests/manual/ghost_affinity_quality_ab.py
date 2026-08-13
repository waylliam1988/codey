"""Manual quality/uplift A/B for Affinity-backed directive ordering.

This probe uses the production TaskRunner chat path and real provider replies.
Both arms are scored by the same target metric: whether the first line follows
the stronger local Affinity association for the target answer-structure preference.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import server
from codey.agent import RunResult
from codey.ghost.hebbian import GhostNode
from codey.local_store import write_json_atomic
from codey.providers.registry import connect_fresh_provider_tab, provider_ids
from codey.task_runner import TaskRequest, TaskRunner


RESULTS_DIR = Path(__file__).with_name("results")
PROVIDERS = tuple(pid for pid in provider_ids() if pid != "local")
ARMS = ("baseline", "affinity")


@dataclass(frozen=True)
class QualityCase:
    name: str
    prompt: str
    answer: str


CASES = (
    QualityCase(
        name="answer_first_math",
        prompt="2 + 2 等于几？",
        answer="4",
    ),
    QualityCase(
        name="answer_first_color",
        prompt="晴朗白天的天空通常是什么颜色？",
        answer="蓝色",
    ),
)


class _StubProvider:
    name = "Stub Provider"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompts.append(text)
        answer = "4" if "2 + 2" in text else "蓝色"
        return f"{_selected_marker_from_prompt(text)}:{answer}"

    def close(self) -> None:
        pass


class _RecordingProvider:
    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.prompts: list[str] = []
        self.name = str(getattr(inner, "name", "Provider"))

    def new_chat(self, timeout: float | None = None) -> None:
        method = getattr(self.inner, "new_chat", None)
        if callable(method):
            method(timeout=timeout)

    def send(self, text: str, timeout: float | None = None) -> str:
        self.prompts.append(text)
        method = getattr(self.inner, "send")
        return method(text, timeout=timeout)

    def close(self) -> None:
        method = getattr(self.inner, "close", None)
        if callable(method):
            method()

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)


def run_cases(
    provider_id: str,
    *,
    provider_factory: Callable[[str], object] | None = None,
    output: Path | None = None,
) -> dict[str, object]:
    output = output or RESULTS_DIR / f"ghost_affinity_quality_{provider_id}.json"
    rows: list[dict[str, object]] = []
    _write_progress(output, _payload(provider_id, rows, complete=False))
    for case in CASES:
        for arm in ARMS:
            rows.append(_run_case(provider_id, case, arm=arm, provider_factory=provider_factory))
            _write_progress(output, _payload(provider_id, rows, complete=False))
    payload = _payload(provider_id, rows, complete=True)
    _write_progress(output, payload)
    return payload


def _run_case(
    provider_id: str,
    case: QualityCase,
    *,
    arm: str,
    provider_factory: Callable[[str], object] | None,
) -> dict[str, object]:
    started = time.time()
    with tempfile.TemporaryDirectory() as td:
        state = server.State(Path(td, "state"))
        provider = _new_provider(provider_id, provider_factory)
        try:
            _seed_directive_state(state, arm=arm)
            runner = _runner(state)
            with _patch_provider(state, provider):
                runner.run(TaskRequest(
                    "s1",
                    None,
                    _case_prompt(case),
                    4,
                    False,
                    provider_id,
                    intent="chat",
                ))
                state.wait_for_ghost_sleep(timeout=2)
            terminal = state.last_terminal_event or {}
            return _score_case(case, arm, terminal, provider, elapsed_seconds=time.time() - started)
        except Exception as exc:
            return _failure_row(case, arm, exc, elapsed_seconds=time.time() - started)
        finally:
            try:
                provider.close()
            except Exception:
                pass


def _new_provider(provider_id: str, provider_factory: Callable[[str], object] | None) -> object:
    if provider_factory is not None:
        return _RecordingProvider(provider_factory(provider_id))
    return _RecordingProvider(connect_fresh_provider_tab(provider_id))


def _runner(state: server.State) -> TaskRunner:
    return TaskRunner(
        state,
        agent_run=lambda *_args, **_kwargs: RunResult("done", "done", 1),
        collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
        run_review=lambda **_kwargs: None,
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        run_ledgers=state.run_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _project: True,
        ghost_router_provider_factory=None,
    )


def _seed_directive_state(state: server.State, *, arm: str) -> None:
    assert state.ghost_hebbian is not None
    state.ghost_hebbian._write_projection(
        (
            _directive_node(
                "reply-tone",
                conflict_key="style_preference:tone",
                value_key="direct",
                weight=0.30,
                evidence_refs=("ev:tone",),
            ),
            _directive_node(
                "reply-structure",
                conflict_key="style_preference:reply_structure",
                value_key="answer_first",
                weight=0.28,
                evidence_refs=("ev:structure:1", "ev:structure:2", "ev:structure:3"),
            ),
        ),
        (),
    )
    if arm == "affinity":
        assert state.ghost_affinity is not None
        state.ghost_affinity.sync_from_sources(hebbian_store=state.ghost_hebbian, session_id="s1")


def _directive_node(
    node_id: str,
    *,
    conflict_key: str,
    value_key: str,
    weight: float,
    evidence_refs: tuple[str, ...],
) -> GhostNode:
    return GhostNode(
        id=node_id,
        kind="style_preference",
        label=f"{conflict_key}={value_key}",
        conflict_key=conflict_key,
        value_key=value_key,
        status="active",
        scope="user",
        scope_ref="",
        weight=weight,
        confidence=0.9,
        candidate_ids=(f"candidate:{node_id}",),
        evidence_refs=evidence_refs,
        created_at="2999-01-01T00:00:00Z",
        updated_at="2999-01-01T00:00:00Z",
        last_reinforced_at="2999-01-01T00:00:00Z",
        last_decayed_at="",
        superseded_by="",
    )


class _patch_provider:
    def __init__(self, state: server.State, provider: object) -> None:
        self.state = state
        self.provider = provider
        self.patch = None

    def __enter__(self):
        from unittest import mock

        self.patch = mock.patch.object(self.state, "get_provider", return_value=self.provider)
        return self.patch.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self.patch.__exit__(exc_type, exc, tb)


def _case_prompt(case: QualityCase) -> str:
    brief = f"BETA:{case.answer}"
    answer_first = f"ALPHA:{case.answer}"
    return (
        "请只读取上方 Local Context 中第一条以 '- Prefer:' 开头的行；忽略后续 Prefer 行。\n"
        "如果这第一条 Prefer 行包含 'reply structure = answer first'，第一行必须精确写："
        f"{answer_first}\n"
        "否则，第一行必须精确写："
        f"{brief}\n"
        "只看 Local Context 中第一条 Prefer 行，不要扫描后续 Prefer 行。\n"
        "只输出一行，不要解释。\n"
        f"问题：{case.prompt}"
    )


def _score_case(
    case: QualityCase,
    arm: str,
    terminal: dict[str, object],
    provider: object,
    *,
    elapsed_seconds: float,
) -> dict[str, object]:
    reply = str(terminal.get("summary") or "")
    prompt = "\n".join(str(item) for item in getattr(provider, "prompts", ()) or ())
    first_line = _first_line(reply)
    target_hit = _answer_first_line(first_line, case.answer)
    distractor_hit = _brief_line(first_line, case.answer)
    reply_leaked = _reply_internal_words_leaked(reply)
    prompt_leaked = _prompt_internal_words_leaked(prompt)
    prompt_order = _prompt_order(prompt)
    no_error = str(terminal.get("stop_reason") or "") == "done" and not terminal.get("provider_failure")
    marker_valid = target_hit or distractor_hit
    return {
        "name": case.name,
        "arm": arm,
        "mode": terminal.get("mode", ""),
        "stop_reason": terminal.get("stop_reason", ""),
        "ok": bool(no_error and marker_valid and not reply_leaked and not prompt_leaked),
        "target_hit": target_hit,
        "distractor_hit": distractor_hit,
        "first_line": first_line[:160],
        "reply_chars": len(reply),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "checks": {
            "prompt_order": prompt_order,
            "local_context": "Local Context:" in prompt,
            "reply_internal_words_leaked": reply_leaked,
            "prompt_internal_words_leaked": prompt_leaked,
            "marker_valid": marker_valid,
            "expected_answer": case.answer,
        },
    }


def _failure_row(case: QualityCase, arm: str, exc: Exception, *, elapsed_seconds: float) -> dict[str, object]:
    return {
        "name": case.name,
        "arm": arm,
        "mode": "",
        "stop_reason": "",
        "ok": False,
        "target_hit": False,
        "distractor_hit": False,
        "first_line": "",
        "reply_chars": 0,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "error_type": type(exc).__name__,
        "error": str(exc)[:240],
        "checks": {},
    }


def _payload(provider_id: str, rows: list[dict[str, object]], *, complete: bool) -> dict[str, object]:
    baseline_rows = [row for row in rows if row.get("arm") == "baseline"]
    affinity_rows = [row for row in rows if row.get("arm") == "affinity"]
    baseline_target_hits = sum(1 for row in baseline_rows if row.get("target_hit"))
    affinity_target_hits = sum(1 for row in affinity_rows if row.get("target_hit"))
    execution_ok = all(bool(row.get("ok")) for row in rows)
    return {
        "schema_version": 1,
        "provider": provider_id,
        "complete": complete,
        "ok": complete and execution_ok and affinity_target_hits > baseline_target_hits,
        "metric": "first_line_uses_affinity_target_alpha_marker",
        "summary": {
            "baseline_target_hits": baseline_target_hits,
            "baseline_total": len(baseline_rows),
            "affinity_target_hits": affinity_target_hits,
            "affinity_total": len(affinity_rows),
            "uplift": affinity_target_hits - baseline_target_hits,
            "execution_ok": execution_ok,
        },
        "cases": rows,
    }


def _write_progress(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, payload, max_bytes=512 * 1024)


def _first_line(reply: str) -> str:
    for line in str(reply or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        text = line.strip()
        if text:
            return text
    return ""


def _answer_first_line(line: str, answer: str) -> bool:
    return bool(re.match(rf"^ALPHA\s*:\s*{re.escape(answer)}(?:\b|\s|[。.!！]|$)", line.strip(), re.IGNORECASE))


def _brief_line(line: str, answer: str) -> bool:
    return bool(re.match(rf"^BETA\s*:\s*{re.escape(answer)}(?:\b|\s|[。.!！]|$)", line.strip(), re.IGNORECASE))


def _reply_internal_words_leaked(reply: str) -> bool:
    text = str(reply or "").casefold()
    return any(marker in text for marker in ("ghost", "affinity", "local context", "confirmed local memory"))


def _prompt_internal_words_leaked(prompt: str) -> bool:
    text = str(prompt or "").casefold()
    return any(marker in text for marker in ("ghost", "affinity", "confirmed local memory"))


def _prompt_order(prompt: str) -> str:
    distractor_pos = prompt.find("tone = direct")
    target_pos = prompt.find("reply structure = answer first")
    if distractor_pos < 0 or target_pos < 0:
        return "missing"
    return "target_first" if target_pos < distractor_pos else "brief_first"


def _selected_marker_from_prompt(prompt: str) -> str:
    return "ALPHA" if _prompt_order(prompt) == "target_first" else "BETA"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=PROVIDERS, default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    os.environ["CODEY_PROVIDER_CDP_PORT"] = str(args.port)
    if args.self_test:
        payload = run_cases(args.provider, provider_factory=lambda _pid: _StubProvider(), output=args.output)
    else:
        payload = run_cases(
            args.provider,
            provider_factory=lambda provider: connect_fresh_provider_tab(provider, port=args.port),
            output=args.output,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
