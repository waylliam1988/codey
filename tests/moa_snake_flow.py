from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.providers import controls as provider_controls
from codey.app import server as codey_server
from codey.workspace.changes import collect_changes, is_git_repository
from codey.agents.consensus import run_project_audit_advisor
from codey.providers.registry import (
    connect_existing_provider,
    provider_tab_availability,
)
from codey.reviews.core import parse_review_with_repair, render_review_prompt


WRITER_ID = "deepseek"
ADVISOR_IDS = ("glm", "stepfun", "qwen")
SLOW_SEND_SECONDS = 120.0

DISCUSSION_TASK = (
    "我们准备从零开始做一个非常克制的浏览器贪吃蛇小游戏。"
    "请先讨论第一版应该怎么做：文件结构、核心交互、测试方式、"
    "以及哪些功能暂时不要做。不要写代码。"
)

CREATE_TASK = (
    "从零开始实现一个经典浏览器贪吃蛇小游戏。"
    "项目当前应该视为空项目。请创建 index.html、style.css、game.js 和 "
    "test_snake_static.py。要求：不依赖外部网络资源；Canvas 绘制；方向键或 WASD 控制；"
    "计分；撞墙或撞自己后 Game Over；可以重新开始；界面简单清楚。"
    "test_snake_static.py 用 Python unittest 做静态检查，覆盖必要文件、Canvas、输入处理、"
    "分数、Game Over、restart 等关键行为线索。完成后运行 python -m unittest，"
    "只有测试通过后再 done。"
)

DIFF_REVIEW_TASK = (
    "Review the completed snake game diff for correctness, simplicity, and whether "
    "the implementation satisfies the requested first version."
)

PROJECT_AUDIT_TASK = (
    "请只读审查这个贪吃蛇项目。重点检查是否有明显 bug、架构是否过度复杂、"
    "是否有不必要功能、测试是否能覆盖核心行为。不要改文件。"
)

AUDIT_FIX_TASK = (
    "以下是多模型只读项目审查报告。请逐条验证，只修复你能确认的具体问题；"
    "如果报告没有有效问题，就不要为了修改而修改。修复后运行 python -m unittest 并 done。\n\n"
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clip(text: object, limit: int = 1200) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(value) <= limit:
        return value
    head = limit // 2
    tail = limit - head - 40
    return value[:head].rstrip() + "\n... [clipped] ...\n" + value[-tail:].lstrip()


@dataclass
class StageResult:
    name: str
    ok: bool
    elapsed: float
    detail: dict


class FlowRecorder:
    def __init__(self, artifacts: str | Path) -> None:
        self.root = Path(artifacts).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / "flow.log"
        self.events_path = self.root / "events.jsonl"
        self.state_path = self.root / "flow_state.json"
        self.results: list[StageResult] = []
        self.current_stage = ""

    def log(self, message: str) -> None:
        line = f"[{_now()}] {message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def event(self, kind: str, **payload) -> None:
        data = {
            "time": _now(),
            "stage": self.current_stage,
            "kind": kind,
            **payload,
        }
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def checkpoint(self, status: str, **payload) -> None:
        data = {
            "time": _now(),
            "status": status,
            "stage": self.current_stage,
            "results": [
                {
                    "name": result.name,
                    "ok": result.ok,
                    "elapsed": round(result.elapsed, 2),
                    "detail": result.detail,
                }
                for result in self.results
            ],
            **payload,
        }
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        previous = self.current_stage
        self.current_stage = name
        started = time.monotonic()
        self.log(f"START {name}")
        self.checkpoint("running")
        try:
            yield
        except Exception as exc:
            elapsed = time.monotonic() - started
            self.results.append(StageResult(
                name=name,
                ok=False,
                elapsed=elapsed,
                detail={"error": str(exc)},
            ))
            self.event(
                "stage_error",
                error=str(exc),
                traceback=traceback.format_exc(limit=12),
                elapsed=round(elapsed, 2),
            )
            self.checkpoint("failed", error=str(exc))
            self.log(f"FAIL {name} after {elapsed:.1f}s: {exc}")
            raise
        else:
            elapsed = time.monotonic() - started
            self.results.append(StageResult(name=name, ok=True, elapsed=elapsed, detail={}))
            self.event("stage_done", elapsed=round(elapsed, 2))
            self.checkpoint("running")
            self.log(f"DONE {name} in {elapsed:.1f}s")
        finally:
            self.current_stage = previous


class TimedProvider:
    def __init__(self, provider, provider_id: str, recorder: FlowRecorder, role: str) -> None:
        self._provider = provider
        self.provider_id = provider_id
        self.recorder = recorder
        self.role = role

    def __getattr__(self, name: str):
        return getattr(self._provider, name)

    @property
    def name(self) -> str:
        return getattr(self._provider, "name", self.provider_id)

    @property
    def location(self) -> str:
        return getattr(self._provider, "location", "")

    def new_chat(self) -> None:
        started = time.monotonic()
        self.recorder.event("provider_new_chat_start", provider=self.provider_id, role=self.role)
        try:
            return self._provider.new_chat()
        finally:
            self.recorder.event(
                "provider_new_chat_done",
                provider=self.provider_id,
                role=self.role,
                elapsed=round(time.monotonic() - started, 2),
            )

    def send(self, text: str, timeout: float | None = None) -> str:
        started = time.monotonic()
        self.recorder.event(
            "provider_send_start",
            provider=self.provider_id,
            role=self.role,
            timeout=timeout,
            prompt_chars=len(text or ""),
            prompt_preview=_clip(text, 500),
        )
        try:
            reply = self._provider.send(text, timeout=timeout)
        except Exception as exc:
            elapsed = time.monotonic() - started
            self.recorder.event(
                "provider_send_error",
                provider=self.provider_id,
                role=self.role,
                elapsed=round(elapsed, 2),
                error=str(exc),
                slow=elapsed >= SLOW_SEND_SECONDS,
            )
            raise
        elapsed = time.monotonic() - started
        self.recorder.event(
            "provider_send_done",
            provider=self.provider_id,
            role=self.role,
            elapsed=round(elapsed, 2),
            reply_chars=len(reply or ""),
            reply_preview=_clip(reply, 500),
            slow=elapsed >= SLOW_SEND_SECONDS,
        )
        return reply

    def close(self) -> None:
        return self._provider.close()


@contextmanager
def patched_server(recorder: FlowRecorder, state_home: Path) -> Iterator[codey_server.State]:
    state = codey_server.State(state_home)
    original_state = codey_server.STATE
    original_connect_provider = codey_server.connect_provider
    original_connect_existing = codey_server.connect_existing_provider
    original_borrow_open_provider = codey_server.borrow_open_provider

    def wrap(provider, provider_id: str, role: str):
        if provider is None:
            return None
        if isinstance(provider, TimedProvider):
            return provider
        return TimedProvider(provider, provider_id, recorder, role)

    def timed_connect_provider(provider_id: str, *args, **kwargs):
        provider = original_connect_provider(provider_id, *args, **kwargs)
        return wrap(provider, provider_id, "writer")

    def timed_connect_existing(provider_id: str):
        provider = original_connect_existing(provider_id)
        return wrap(provider, provider_id, "advisor")

    def timed_borrow_open_provider(provider_id: str, owner_page):
        provider = original_borrow_open_provider(provider_id, owner_page)
        return wrap(provider, provider_id, "borrowed-advisor")

    try:
        codey_server.STATE = state
        codey_server.connect_provider = timed_connect_provider
        codey_server.connect_existing_provider = timed_connect_existing
        codey_server.borrow_open_provider = timed_borrow_open_provider
        provider_controls.set_teach_handler(state.handle_control_teach)
        provider_controls.set_doctor_handler(state.handle_profile_doctor)
        yield state
    finally:
        codey_server.STATE = original_state
        codey_server.connect_provider = original_connect_provider
        codey_server.connect_existing_provider = original_connect_existing
        codey_server.borrow_open_provider = original_borrow_open_provider
        provider_controls.set_teach_handler(original_state.handle_control_teach)
        provider_controls.set_doctor_handler(original_state.handle_profile_doctor)


def reset_project(project: Path, artifacts: Path) -> Path | None:
    project = project.resolve()
    artifacts = artifacts.resolve()
    project.mkdir(parents=True, exist_ok=True)
    entries = [
        entry
        for entry in project.iterdir()
        if entry.name != ".codey" and entry.resolve() != artifacts
    ]
    if not entries:
        return None
    backup = artifacts / "backup" / project.name
    if backup.exists():
        shutil.rmtree(backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project, backup, ignore=shutil.ignore_patterns(".codey"))
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    return backup


def run_python_unittest(project: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest"],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    output = "\n".join(part for part in (proc.stdout.rstrip(), proc.stderr.rstrip()) if part)
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": output[-4000:],
    }


def verify_snake_project(project: Path) -> dict:
    required = ("index.html", "style.css", "game.js", "test_snake_static.py")
    missing = [name for name in required if not (project / name).is_file()]
    details: dict[str, object] = {"missing": missing}
    if missing:
        return {"ok": False, **details}

    combined = "\n".join(
        (project / name).read_text(encoding="utf-8", errors="replace")
        for name in ("index.html", "style.css", "game.js")
    ).lower()
    markers = {
        "canvas": "canvas" in combined,
        "keyboard": "keydown" in combined or "wasd" in combined,
        "score": "score" in combined,
        "game_over": "game over" in combined or "gameover" in combined,
        "restart": "restart" in combined or "reset" in combined,
    }
    unittest_result = run_python_unittest(project)
    return {
        "ok": all(markers.values()) and unittest_result["ok"],
        **details,
        "markers": markers,
        "unittest": unittest_result,
    }


def run_task(
    *,
    state: codey_server.State,
    recorder: FlowRecorder,
    session_id: str,
    project: Path | None,
    task: str,
    max_turns: int,
    continue_task: bool = False,
) -> dict:
    codey_server._run_task(
        session_id=session_id,
        project=str(project) if project is not None else None,
        task=task,
        max_turns=max_turns,
        continue_task=continue_task,
        provider_id=WRITER_ID,
    )
    terminal = state.last_terminal_event or {}
    recorder.event("task_terminal", terminal=terminal)
    if terminal.get("stop_reason") != "done":
        raise RuntimeError(f"task failed: {terminal.get('summary') or terminal}")
    return terminal


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def run_reviewer_matrix(project: Path, recorder: FlowRecorder) -> list[dict]:
    changes = collect_changes(project, codey_server.STATE.change_tracker_for(
        str(project.resolve()),
        persistent=not is_git_repository(project),
    ))
    results: list[dict] = []
    for provider_id in ADVISOR_IDS:
        reviewer = None
        started = time.monotonic()
        try:
            reviewer = TimedProvider(
                connect_existing_provider(provider_id),
                provider_id,
                recorder,
                "explicit-reviewer",
            )
            reviewer.new_chat()
            prompt = render_review_prompt(
                project=str(project),
                task=DIFF_REVIEW_TASK,
                writer_summary="Snake game implementation completed by DeepSeek.",
                changes=changes,
                recent_log="",
            )
            with provider_controls.suppress_assistance():
                reply = reviewer.send(prompt, timeout=codey_server.REVIEW_TIMEOUT)
                review = parse_review_with_repair(
                    reply,
                    lambda repair: reviewer.send(repair, timeout=codey_server.REVIEW_TIMEOUT),
                )
            item = {
                "provider": provider_id,
                "ok": True,
                "approved": review.approved,
                "summary": review.summary,
                "findings": [
                    {
                        "path": finding.path,
                        "issue": finding.issue,
                        "suggested_fix": finding.suggested_fix,
                    }
                    for finding in review.findings
                ],
                "elapsed": round(time.monotonic() - started, 2),
            }
        except Exception as exc:
            item = {
                "provider": provider_id,
                "ok": False,
                "error": str(exc),
                "elapsed": round(time.monotonic() - started, 2),
            }
        finally:
            if reviewer is not None:
                try:
                    reviewer.close()
                except Exception:
                    pass
        recorder.event("explicit_review_result", **item)
        results.append(item)
    return results


def run_project_audit_matrix(project: Path, recorder: FlowRecorder) -> list[dict]:
    results: list[dict] = []
    for provider_id in ADVISOR_IDS:
        advisor = None
        started = time.monotonic()
        try:
            advisor = TimedProvider(
                connect_existing_provider(provider_id),
                provider_id,
                recorder,
                "explicit-project-audit",
            )
            advisor.new_chat()
            text = run_project_audit_advisor(
                advisor,
                project,
                PROJECT_AUDIT_TASK,
                context="Snake project smoke after initial implementation.",
            )
            item = {
                "provider": provider_id,
                "ok": bool(text.strip()),
                "report": text,
                "elapsed": round(time.monotonic() - started, 2),
            }
        except Exception as exc:
            item = {
                "provider": provider_id,
                "ok": False,
                "error": str(exc),
                "elapsed": round(time.monotonic() - started, 2),
            }
        finally:
            if advisor is not None:
                try:
                    advisor.close()
                except Exception:
                    pass
        recorder.event("project_audit_result", **item)
        results.append(item)
    return results


def audit_followup(reviews: list[dict], audits: list[dict]) -> str:
    lines = [AUDIT_FIX_TASK, "Diff review results:"]
    for item in reviews:
        lines.append(f"- {item.get('provider')}: {json.dumps(item, ensure_ascii=False)[:3000]}")
    lines.append("")
    lines.append("Project audit reports:")
    for item in audits:
        report = item.get("report") or item.get("error") or ""
        lines.append(f"- {item.get('provider')}: {_clip(report, 3000)}")
    return "\n".join(lines)


def summarize_bottlenecks(recorder: FlowRecorder) -> list[dict]:
    events = read_jsonl(recorder.events_path)
    slow = [
        {
            "stage": event.get("stage"),
            "provider": event.get("provider"),
            "role": event.get("role"),
            "elapsed": event.get("elapsed"),
            "kind": event.get("kind"),
        }
        for event in events
        if event.get("kind") in {"provider_send_done", "provider_send_error"}
        and float(event.get("elapsed") or 0) >= SLOW_SEND_SECONDS
    ]
    errors = [
        {
            "stage": event.get("stage"),
            "provider": event.get("provider"),
            "role": event.get("role"),
            "error": event.get("error"),
            "kind": event.get("kind"),
        }
        for event in events
        if str(event.get("kind") or "").endswith("_error")
        or event.get("kind") == "stage_error"
    ]
    return slow + errors


def run_flow(args: argparse.Namespace) -> dict:
    project = Path(args.project).expanduser().resolve()
    artifact_root = Path(args.artifacts).resolve()
    recorder = FlowRecorder(artifact_root)
    recorder.log(f"Project: {project}")
    recorder.log(f"Artifacts: {artifact_root}")

    statuses = provider_tab_availability()
    recorder.event("provider_availability", statuses=statuses)
    missing = [provider_id for provider_id in (WRITER_ID, *ADVISOR_IDS) if not statuses.get(provider_id)]
    if missing:
        raise RuntimeError(f"provider tabs are not open: {', '.join(missing)}")

    if args.reset:
        with recorder.stage("reset_project"):
            backup = reset_project(project, artifact_root)
            recorder.event("project_reset", backup=str(backup) if backup else "")

    state_home = artifact_root / "state"
    with patched_server(recorder, state_home) as state:
        with recorder.stage("new_chat_moa_discussion"):
            terminal = run_task(
                state=state,
                recorder=recorder,
                session_id="moa-snake-chat",
                project=None,
                task=DISCUSSION_TASK,
                max_turns=args.max_turns,
            )
            recorder.event("discussion_done", terminal=terminal)

        with recorder.stage("write_snake_from_zero"):
            terminal = run_task(
                state=state,
                recorder=recorder,
                session_id="moa-snake-project",
                project=project,
                task=CREATE_TASK,
                max_turns=args.max_turns,
            )
            recorder.event("create_done", terminal=terminal)

        with recorder.stage("independent_verification_after_create"):
            verification = verify_snake_project(project)
            recorder.event("verification", result=verification)
            if not verification["ok"]:
                raise RuntimeError("independent verification failed after create")

        with recorder.stage("explicit_diff_review_matrix"):
            review_results = run_reviewer_matrix(project, recorder)
            if not any(item.get("ok") for item in review_results):
                raise RuntimeError("all explicit diff reviewers failed")

        with recorder.stage("explicit_project_audit_matrix"):
            audit_results = run_project_audit_matrix(project, recorder)
            if not any(item.get("ok") for item in audit_results):
                raise RuntimeError("all explicit project audits failed")

        with recorder.stage("writer_followup_after_multi_model_audit"):
            terminal = run_task(
                state=state,
                recorder=recorder,
                session_id="moa-snake-project",
                project=project,
                task=audit_followup(review_results, audit_results),
                max_turns=args.max_turns,
                continue_task=True,
            )
            recorder.event("followup_done", terminal=terminal)

        with recorder.stage("independent_verification_after_followup"):
            verification = verify_snake_project(project)
            recorder.event("verification", result=verification)
            if not verification["ok"]:
                raise RuntimeError("independent verification failed after followup")

    result = {
        "ok": True,
        "project": str(project),
        "artifacts": str(artifact_root),
        "stages": [
            {
                "name": item.name,
                "ok": item.ok,
                "elapsed": round(item.elapsed, 2),
                "detail": item.detail,
            }
            for item in recorder.results
        ],
        "bottlenecks": summarize_bottlenecks(recorder),
        "verification": verify_snake_project(project),
    }
    recorder.checkpoint("complete", final=result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=r"E:\snake")
    parser.add_argument("--artifacts", default="")
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if not args.artifacts:
            args.artifacts = str(Path(args.project).expanduser().resolve() / ".codey" / "smoke" / "moa-snake-flow")
        result = run_flow(args)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("PASS" if result.get("ok") else f"FAIL: {result.get('error', '')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())