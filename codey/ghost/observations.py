"""Durable experience observations for Ghost retrieval-based memory.

终态说明（经历记忆 + 下轮检索）：

- 默认检索不发起额外模型调用。明确配置 embedding endpoint 后，语义检索会
  对待索引原话和当前查询调用该端点；向量与原话仍归独立 ghost.sqlite3 所有。
- 这里只保存已发生的回合事实（用户原话、最终回答、任务结果），读取是
  同步有界扫描（最近 200 条内检索，全文件至多 5000 条），没有后台索引
  线程；下一次正常模型调用前检索少量相关原话并判断。语义判断发生在
  本来就要回答的那次调用里。
- 观察记录以 ``run_id`` 幂等：同 ``run_id`` 重放覆盖，不产生第二条经历；
  不同 ``run_id`` 即使内容相似也保留为不同经历，不做强化合并。现有
  inbox/hebbian 的自动结构化更新已停止。用户可在控制面板从已提交经历
  显式提出偏好并审核，但原话记录不会暗中变成已确认的偏好。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from codey.ghost import _common
from codey.ghost.embeddings import Embeddings, configured_embeddings
from codey.ghost.schema import clip_signal_text
from codey.ghost.sqlite import GhostSQLite
from codey.ghost.vectors import GhostVectorMemory, observation_digest
from codey.storage.local_store import DEFAULT_STATE_HOME

OBSERVATIONS_SCHEMA_VERSION = 1
MAX_OBSERVATIONS = 5_000
MAX_USER_TEXT_CHARS = 3_000
MAX_ASSISTANT_TEXT_CHARS = 4_000
MAX_OBSERVATION_DIAGNOSTICS = 4

OBSERVABLE_MODES = ("chat", "agent", "research", "hybrid", "project", "planning", "review")
COMMITTED_STOP_REASONS = ("done",)


class GhostObservationCorruptedError(OSError):
    """Observation log is unreadable (mid-file corruption)."""


class GhostObservationStore:
    """Append-by-run_id experience log with committed gating.

    只有 ``committed=True``（结算写入且 ``stop_reason=done``）的记录可被检索。
    写入失败允许回答继续，但调用方必须记 ``ghost_observation_failed`` 告警，
    不得声称该回合仍可恢复。
    """

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME, *, embeddings: Embeddings | None = None) -> None:
        self.directory = Path(state_home) / "ghost"
        self.database = GhostSQLite(state_home)
        self.database_path = self.database.path
        self.path = self.database_path
        self.last_warnings: tuple[str, ...] = ()
        selected = embeddings if embeddings is not None else configured_embeddings()
        self.vector_memory = GhostVectorMemory(self.database, selected) if selected is not None else None

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
        cleaned_mode = (mode or "").strip().lower() or "chat"
        if cleaned_mode not in OBSERVABLE_MODES:
            cleaned_mode = "chat"
        cleaned_stop = (stop_reason or "").strip().lower()
        row: dict[str, object] = {
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
        try:
            with self.database.connection() as database:
                database.execute(
                    """
                    INSERT INTO observation_events(
                        run_id, session_id, project, mode, user_text,
                        assistant_text, stop_reason, committed, provider_id, ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        session_id=excluded.session_id,
                        project=excluded.project,
                        mode=excluded.mode,
                        user_text=excluded.user_text,
                        assistant_text=excluded.assistant_text,
                        stop_reason=excluded.stop_reason,
                        committed=excluded.committed,
                        provider_id=excluded.provider_id,
                        ts=excluded.ts
                    """,
                    (
                        str(row["run_id"]), str(row["session_id"]), str(row["project"]),
                        str(row["mode"]), str(row["user_text"]), str(row["assistant_text"]),
                        str(row["stop_reason"]), int(bool(row["committed"])),
                        str(row["provider_id"]), str(row["ts"]),
                    ),
                )
                database.execute(
                    "DELETE FROM memory_embeddings WHERE run_id=? AND content_digest<>?",
                    (row["run_id"], observation_digest(row)),
                )
                database.execute(
                    """
                    DELETE FROM observation_events
                    WHERE sequence IN (
                        SELECT sequence FROM observation_events
                        ORDER BY sequence DESC LIMIT -1 OFFSET ?
                    )
                    """,
                    (MAX_OBSERVATIONS,),
                )
            return True
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return False

    def read_all(self) -> tuple[dict[str, object], ...]:
        with self.database.connection() as database:
            rows = database.execute(
                """
                SELECT run_id, session_id, project, mode, user_text,
                       assistant_text, stop_reason, committed, provider_id, ts
                FROM observation_events ORDER BY sequence
                """
            ).fetchall()
        return tuple(_row_payload(row) for row in rows)

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
        normalized_scope = (scope or "session").strip().lower()
        if normalized_scope not in {"session", "project", "user"}:
            raise ValueError("scope must be session, project, or user")
        wanted_session = clip_signal_text(session_id, 120)
        wanted_project = _common.normalize_project(project)
        if normalized_scope == "project" and not wanted_project:
            raise ValueError("project is required for project scope retrieval")
        clauses = ["committed = 1"]
        values: list[object] = []
        if normalized_scope == "project":
            clauses.append("project = ?")
            values.append(wanted_project)
        elif normalized_scope == "session":
            if wanted_session:
                clauses.append("session_id = ?")
                values.append(wanted_session)
            if wanted_project:
                clauses.append("project = ?")
                values.append(wanted_project)
        values.append(max(1, (limit or 1)))
        with self.database.connection() as database:
            rows = database.execute(
                "SELECT run_id, session_id, project, mode, user_text, assistant_text, "
                "stop_reason, committed, provider_id, ts FROM observation_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return tuple(_row_payload(row) for row in rows)

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> int:
        normalized_scope = (scope or "").strip().lower()
        if normalized_scope not in {"user", "project", "session"}:
            raise ValueError("scope must be user, project, or session")
        normalized_project = _common.normalize_project(project)
        normalized_session = clip_signal_text(session_id, 120)
        if normalized_scope == "project" and not normalized_project:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not normalized_session:
            raise ValueError("session_id is required for session scope deletion")
        clauses: list[str] = []
        values: list[object] = []
        if normalized_scope == "user":
            clauses.append("1 = 1")
        elif normalized_scope == "project":
            clauses.append("project = ?")
            values.append(normalized_project)
        else:
            clauses.append("session_id = ?")
            values.append(normalized_session)
        with self.database.connection() as database:
            result = database.execute(
                "DELETE FROM observation_events WHERE " + " AND ".join(clauses), tuple(values)
            )
            return int(result.rowcount)

    def retrieve(
        self, query: str, *, session_id: str = "", project: str = "", scope: str = "session",
        exclude_run_id: str = "", max_items: int = 4, budget_chars: int = 2000,
    ) -> tuple[dict[str, object], ...]:
        from codey.ghost.observation_index import fit_observation_rows, retrieve_relevant_observations

        rows = tuple(row for row in self.read_committed(session_id=session_id, project=project, scope=scope)
                     if str(row["run_id"]) != exclude_run_id)
        if self.vector_memory is not None:
            ranked = self.vector_memory.rank(query, rows, limit=max_items)
            return fit_observation_rows(ranked, budget_chars=budget_chars, max_items=max_items)
        return retrieve_relevant_observations(rows, query, max_items=max_items, budget_chars=budget_chars)

    def reset_all(self) -> None:
        with self.database.connection() as database:
            database.execute("DELETE FROM observation_events")

    def export_state(self) -> dict[str, object]:
        return {
            "schema_version": OBSERVATIONS_SCHEMA_VERSION,
            "observations": list(self.read_all()),
        }

    def close(self) -> None:
        """Operations own and close their individual connections."""


def _row_payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "schema_version": OBSERVATIONS_SCHEMA_VERSION,
        "ts": str(row["ts"]),
        "type": "ghost_observation",
        "run_id": str(row["run_id"]),
        "session_id": str(row["session_id"]),
        "project": str(row["project"]),
        "mode": str(row["mode"]),
        "user_text": str(row["user_text"]),
        "assistant_text": str(row["assistant_text"]),
        "stop_reason": str(row["stop_reason"]),
        "committed": bool(row["committed"]),
        "provider_id": str(row["provider_id"]),
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
    "GhostObservationCorruptedError",
    "GhostObservationStore",
]
