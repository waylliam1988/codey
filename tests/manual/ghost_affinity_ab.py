"""Manual production-spine A/B for 0.3.10 Affinity Index.

The self-test path uses stubs and writes atomic partial progress. Real runs use
the production TaskRunner path and provider tabs, while mode bodies remain
safe stubs so this probe does not edit files or run shell commands.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import server
from codey.agent import RunResult
from codey.ghost.hebbian import GhostNode
import codey.ghost.work_queue as work_queue_module
from codey.knowledge.research_interest import ResearchInterestCandidate
from codey.providers.registry import connect_fresh_provider_tab, provider_ids
from codey.research.pipeline import ResearchIterationRun
from codey.research.runner import ResearchRunResult
from codey.task_runner import TaskRequest, TaskRunner
from codey.local_store import write_json_atomic


RESULTS_DIR = Path(__file__).with_name("results")
PROVIDERS = tuple(pid for pid in provider_ids() if pid != "local")
ARMS = ("baseline", "affinity")


@dataclass(frozen=True)
class AffinityCase:
    name: str
    kind: str
    prompt: str


CASES = (
    AffinityCase("chat_directive_order", "chat", "请按当前偏好回答：用什么回复格式？"),
    AffinityCase("work_queue_continue_order", "work", "continue"),
    AffinityCase("research_interest_priority", "research", "continue"),
    AffinityCase("explicit_override", "explicit", "用 chat 模式回答，不要切 provider。"),
    AffinityCase("permission_boundary", "permission", "continue"),
)


class _StubProvider:
    name = "Stub Provider"

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompts.append(text)
        return self.reply

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
    output = output or RESULTS_DIR / f"ghost_affinity_{provider_id}.json"
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
    case: AffinityCase,
    *,
    arm: str,
    provider_factory: Callable[[str], object] | None,
) -> dict[str, object]:
    started = time.time()
    with tempfile.TemporaryDirectory() as td:
        state = server.State(Path(td, "state"))
        provider = _new_provider(provider_id, provider_factory)
        try:
            runner = _runner(state)
            _seed_case(state, case, arm=arm)
            if case.kind in {"work", "research", "permission"}:
                runner._run_research_iteration = lambda **_kwargs: ResearchIterationRun(
                    result=ResearchRunResult(
                        "q",
                        "researched with citation [1]",
                        "done",
                        1,
                        synthesis_id=f"{case.name}-synthesis",
                        citation_map=[{"claim": "x"}],
                    )
                )
            with _patch_provider(state, provider):
                intent = "chat" if case.kind == "explicit" else "auto"
                runner.run(TaskRequest("s1", None, case.prompt, 8, False, provider_id, intent=intent))
                state.wait_for_ghost_sleep(timeout=2)
            terminal = state.last_terminal_event or {}
            return _score_case(case, arm, state, terminal, provider, elapsed_seconds=time.time() - started)
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


def _seed_case(state: server.State, case: AffinityCase, *, arm: str) -> None:
    assert state.ghost_affinity is not None
    if case.kind == "chat":
        _seed_directive_case(state, arm=arm)
    if case.kind in {"work", "research", "permission"}:
        assert state.ghost_work_queue is not None
        item_a = _work_item("alpha", 0.50)
        item_b = _work_item("beta", 0.52)
        state.ghost_work_queue._replace_items([item_a, item_b], f"manual_{case.name}")
        if arm == "affinity":
            state.ghost_affinity.sync_from_sources(
                research_interest_candidates=(_research_candidate("alpha"),),
                session_id="s1",
            )


def _seed_directive_case(state: server.State, *, arm: str) -> None:
    assert state.ghost_hebbian is not None
    state.ghost_hebbian._write_projection(
        (
            _directive_node(
                "reply-length",
                conflict_key="style_preference:reply_length",
                value_key="brief",
                weight=0.30,
                evidence_refs=("ev:length",),
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


def _research_candidate(concept: str) -> ResearchInterestCandidate:
    return ResearchInterestCandidate(
        id=f"ric-{concept}",
        question=f"Research whether {concept} should be tracked",
        related_concepts=(concept,),
        shared_neighbors=(),
        source_refs=(f"note:{concept}",),
        scope="session",
        scope_ref="s1",
        priority=0.72,
        confidence=0.85,
        why_now="Manual bounded affinity A/B.",
        source="concept_open_question",
        source_ref=f"concept:{concept}",
        strong_support=True,
    )


def _work_item(concept: str, priority: float):
    return work_queue_module._new_item(
        kind="research",
        status="queued",
        scope="session",
        scope_ref=work_queue_module._session_ref("s1"),
        title=f"Research {concept} provider recovery",
        why_now="Manual bounded affinity A/B.",
        priority=priority,
        confidence=0.86,
        source="research_interest",
        source_ref=f"manual:{concept}",
        evidence_refs=(f"research_interest:{concept}",),
        run_refs=(),
        now="2999-01-01T00:00:00Z",
        metadata={"related_concepts": [concept]},
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


def _score_case(
    case: AffinityCase,
    arm: str,
    state: server.State,
    terminal: dict[str, object],
    provider: object,
    *,
    elapsed_seconds: float,
) -> dict[str, object]:
    row = {
        "name": case.name,
        "arm": arm,
        "kind": case.kind,
        "mode": terminal.get("mode", ""),
        "stop_reason": terminal.get("stop_reason", ""),
        "ok": False,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "checks": {},
    }
    if case.kind == "chat":
        prompt = "\n".join(str(item) for item in getattr(provider, "prompts", ()) or ())
        length_pos = prompt.find("reply length = brief")
        structure_pos = prompt.find("reply structure = answer first")
        internal_words_leaked = "Ghost" in prompt or "Affinity" in prompt
        row["checks"] = {
            "local_context": "Local Context:" in prompt,
            "reply_length_pos": length_pos,
            "reply_structure_pos": structure_pos,
            "internal_words_leaked": internal_words_leaked,
        }
        expected_order = (
            length_pos >= 0 and structure_pos >= 0 and length_pos < structure_pos
            if arm == "baseline"
            else length_pos >= 0 and structure_pos >= 0 and structure_pos < length_pos
        )
        row["ok"] = terminal.get("mode") == "chat" and expected_order and not internal_words_leaked
    elif case.kind in {"work", "research"}:
        assert state.ghost_work_queue is not None
        items = {item.title: item.status for item in state.ghost_work_queue.list_items(session_id="s1")}
        row["checks"] = {"items": items}
        if arm == "baseline":
            row["ok"] = (
                items.get("Research beta provider recovery") == "done"
                and items.get("Research alpha provider recovery") == "queued"
            )
        else:
            row["ok"] = (
                items.get("Research alpha provider recovery") == "done"
                and items.get("Research beta provider recovery") == "queued"
            )
    elif case.kind == "explicit":
        row["ok"] = terminal.get("mode") == "chat"
    elif case.kind == "permission":
        row["ok"] = terminal.get("mode") == "research" and not terminal.get("provider_failure")
    return row


def _failure_row(case: AffinityCase, arm: str, exc: Exception, *, elapsed_seconds: float) -> dict[str, object]:
    return {
        "name": case.name,
        "arm": arm,
        "kind": case.kind,
        "mode": "",
        "stop_reason": "",
        "ok": False,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "error_type": type(exc).__name__,
        "error": str(exc)[:240],
        "checks": {},
    }


def _payload(provider_id: str, rows: list[dict[str, object]], *, complete: bool) -> dict[str, object]:
    affinity_rows = [row for row in rows if row.get("arm") == "affinity"]
    baseline_rows = [row for row in rows if row.get("arm") == "baseline"]
    return {
        "schema_version": 1,
        "provider": provider_id,
        "complete": complete,
        "ok": complete and all(bool(row.get("ok")) for row in rows),
        "summary": {
            "baseline_ok": sum(1 for row in baseline_rows if row.get("ok")),
            "baseline_total": len(baseline_rows),
            "affinity_ok": sum(1 for row in affinity_rows if row.get("ok")),
            "affinity_total": len(affinity_rows),
        },
        "cases": rows,
    }


def _write_progress(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, payload, max_bytes=512 * 1024)


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
