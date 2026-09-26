"""Durable experience observations for Ghost retrieval-based memory.

终态说明（经历记忆 + 下轮检索）：

- Ghost 不再为每轮发起额外的模型调用，也不要求模型每轮输出
  ``ghost_signals`` 隐藏字段。普通单轮 chat 只有一次正常主推理；
  research/agent/consensus 的多轮调用是任务本身需要的，不是 Ghost 的。
- 这里只保存已发生的回合事实（用户原话、最终回答、任务结果），读取是
  同步有界扫描（最近 200 条内检索，全文件至多 5000 条），没有后台索引
  线程；下一次正常模型调用前检索少量相关原话并判断。语义判断发生在
  本来就要回答的那次调用里。
- 观察记录以 ``run_id`` 幂等：同 ``run_id`` 重放覆盖，不产生第二条经历；
  不同 ``run_id`` 即使内容相似也保留为不同经历，不做强化合并。现有
  inbox/hebbian 的自动结构化更新在本终态下停止（见 control_surface 文档），
  不把原话记录暗中当成已抽取的偏好。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codey.ghost import _common
from codey.ghost.event_log import GhostEventLog
from codey.ghost.schema import clip_signal_text
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME

OBSERVATIONS_SCHEMA_VERSION = 1
MAX_OBSERVATIONS = 5_000
MAX_USER_TEXT_CHARS = 3_000
MAX_ASSISTANT_TEXT_CHARS = 4_000
MAX_OBSERVATION_DIAGNOSTICS = 4

OBSERVABLE_MODES = ("chat", "agent", "research", "hybrid", "project", "planning", "review")
COMMITTED_STOP_REASONS = ("done",)


@dataclass(frozen=True)
class GhostObservation:
    run_id: str
    session_id: str
    project: str
    mode: str
    user_text: str
    assistant_text: str
    stop_reason: str
    committed: bool
    provider_id: str = ""


class GhostObservationStore:
    """Append-by-run_id experience log with committed gating.

    只有 ``committed=True``（结算写入且 ``stop_reason=done``）的记录可被检索。
    写入失败允许回答继续，但调用方必须记 ``ghost_observation_failed`` 告警，
    不得声称该回合仍可恢复。
    """

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "ghost"
        self.path = self.directory / "observations.jsonl"
        self.log = GhostEventLog(
            self.path,
            schema_version=OBSERVATIONS_SCHEMA_VERSION,
            source_name="observations.jsonl",
            allowed_event_kinds=("ghost_observation",),
            bad_row_policy="quarantine_tail",
        )
        self.last_warnings: tuple[str, ...] = ()

    def append_completed(
        self,
        *,
        run_id: str,
        session_id: str,
        project: str = "",
        mode: str,
        user_text: str,
        assistant_text: str,
        stop_reason: str,
        provider_id: str = "",
    ) -> bool:
        cleaned_mode = str(mode or "").strip().lower() or "chat"
        if cleaned_mode not in OBSERVABLE_MODES:
            cleaned_mode = "chat"
        cleaned_stop = str(stop_reason or "").strip().lower()
        row = {
            "schema_version": OBSERVATIONS_SCHEMA_VERSION,
            "ts": _common.now_iso_z(),
            "type": "ghost_observation",
            "run_id": clip_signal_text(run_id, 120),
            "session_id": clip_signal_text(session_id, 120),
            "project": _common.normalize_project(project),
            "mode": cleaned_mode,
            "user_text": clip_signal_text(user_text, MAX_USER_TEXT_CHARS),
            "assistant_text": clip_signal_text(assistant_text, MAX_ASSISTANT_TEXT_CHARS),
            "stop_reason": clip_signal_text(cleaned_stop, 40),
            "committed": cleaned_stop in COMMITTED_STOP_REASONS,
            "provider_id": clip_signal_text(provider_id, 80),
        }
        if not row["run_id"] or not row["session_id"]:
            return False
        # run_id 幂等：同 run 重放覆盖，不新增第二条。
        # 读取被阻断（中间坏行）时绝不写回：空列表写回会丢弃已有记录。
        # 调用方（结算）把 False 记为 ghost_observation_failed 告警。
        with with_file_lock(self.path):
            read = self.log.read_locked()
            self.last_warnings = read.warnings
            if read.blocked:
                return False
            rows = [r for r in read.rows if str(r.get("run_id") or "") != row["run_id"]]
            rows.append(row)
            rows = rows[-MAX_OBSERVATIONS:]
            try:
                self.log.write_atomic_locked(rows)
            except (OSError, TypeError, ValueError):
                return False
        return True

    def read_all(self) -> tuple[dict[str, object], ...]:
        read = self.log.read()
        self.last_warnings = read.warnings
        return tuple(read.rows)

    def read_committed(
        self,
        *,
        session_id: str = "",
        project: str = "",
        limit: int = 200,
        scope: str = "session",
    ) -> tuple[dict[str, object], ...]:
        """Read retrievable rounds under an explicit ownership scope.

        - ``session``: this conversation only (default; never mixes in other
          sessions' rounds). Empty filters stay permissive as before.
        - ``project``: same normalized project across sessions; project is
          required so one project's rounds never leak into another's.
        - ``user``: every committed round in this local store.
        """
        normalized_scope = str(scope or "session").strip().lower()
        if normalized_scope not in {"session", "project", "user"}:
            raise ValueError("scope must be session, project, or user")
        rows = list(self.read_all())
        wanted_session = clip_signal_text(session_id, 120)
        wanted_project = _common.normalize_project(project)
        if normalized_scope == "project" and not wanted_project:
            raise ValueError("project is required for project scope retrieval")
        out: list[dict[str, object]] = []
        for row in reversed(rows):
            if row.get("committed") is not True:
                continue
            if normalized_scope == "project":
                if _common.normalize_project(row.get("project")) != wanted_project:
                    continue
            elif normalized_scope == "session":
                if wanted_session and clip_signal_text(row.get("session_id"), 120) != wanted_session:
                    # 跨会话不召回，避免把别人的经历带入本轮。
                    continue
                if wanted_project and _common.normalize_project(row.get("project")) != wanted_project:
                    continue
            out.append(row)
            if len(out) >= max(1, int(limit or 1)):
                break
        return tuple(out)

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> int:
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"user", "project", "session"}:
            raise ValueError("scope must be user, project, or session")
        normalized_project = _common.normalize_project(project)
        normalized_session = clip_signal_text(session_id, 120)
        if normalized_scope == "project" and not normalized_project:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not normalized_session:
            raise ValueError("session_id is required for session scope deletion")
        with with_file_lock(self.path):
            read = self.log.read_locked()
            self.last_warnings = read.warnings
            if read.blocked:
                # 读取被阻断时不写回：保持原文件，调用方以 warnings 为准。
                return 0
            kept: list[dict[str, object]] = []
            removed = 0
            for row in read.rows:
                if _observation_scope_match(
                    row,
                    normalized_scope,
                    project=normalized_project,
                    session_id=normalized_session,
                ):
                    removed += 1
                else:
                    kept.append(row)
            if removed:
                self.log.write_atomic_locked(kept)
            return removed

    def reset_all(self) -> None:
        self.log.delete()

    def export_state(self) -> dict[str, object]:
        return {
            "schema_version": OBSERVATIONS_SCHEMA_VERSION,
            "observations": list(self.read_all()),
        }


def _observation_scope_match(
    row: dict[str, object],
    scope: str,
    *,
    project: str,
    session_id: str,
) -> bool:
    if scope == "user":
        return True
    if scope == "project":
        return bool(project) and _common.normalize_project(row.get("project")) == project
    return bool(session_id) and clip_signal_text(row.get("session_id"), 120) == session_id


__all__ = [
    "COMMITTED_STOP_REASONS",
    "MAX_ASSISTANT_TEXT_CHARS",
    "MAX_OBSERVATIONS",
    "MAX_USER_TEXT_CHARS",
    "OBSERVABLE_MODES",
    "OBSERVATIONS_SCHEMA_VERSION",
    "GhostObservation",
    "GhostObservationStore",
]
