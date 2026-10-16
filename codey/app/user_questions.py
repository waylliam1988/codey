"""Durable choice decisions. Answer acceptance precedes queue delivery."""
from __future__ import annotations

import builtins
import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from codey.storage.conversation_db import conversation_database, initialize_conversation_database
from codey.task.questions import question_args


class UserQuestionStore:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "conversation_questions.sqlite3"
        initialize_conversation_database(self.path, "CREATE TABLE IF NOT EXISTS questions (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.lock = threading.RLock()

    def get(self, identity: str) -> dict[str, Any] | None:
        with self.lock, conversation_database(self.path) as database:
            row = database.execute("SELECT payload FROM questions WHERE id=?", (identity,)).fetchone()
            return json.loads(row[0]) if row else None

    def list(self) -> builtins.list[dict[str, Any]]:
        with self.lock, conversation_database(self.path) as database:
            return [json.loads(row[0]) for row in database.execute("SELECT payload FROM questions WHERE json_extract(payload, '$.state') IN ('pending', 'answer_accepted') OR rowid IN (SELECT rowid FROM questions ORDER BY rowid DESC LIMIT 256) ORDER BY rowid DESC")]

    def pending(self, session_id: str) -> dict[str, Any] | None:
        with self.lock, conversation_database(self.path) as database:
            row = database.execute("SELECT payload FROM questions WHERE json_extract(payload, '$.session_id')=? AND json_extract(payload, '$.state')='pending' ORDER BY rowid DESC LIMIT 1", (session_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def _save(self, item: dict[str, Any]) -> dict[str, Any]:
        with conversation_database(self.path) as database:
            database.execute("INSERT INTO questions(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                                  (item["id"], json.dumps(item, ensure_ascii=False)))
        return item

    def request(self, identity: str, session_id: str, run_id: str, question: dict[str, Any], continuation: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            content = question_args(question)
            old = self.get(identity)
            if old:
                if old["session_id"] != session_id or old["run_id"] != run_id or any(old[key] != value for key, value in content.items()):
                    raise ValueError("question identity conflict")
                return old
            return self._save({"schema_version": 1, "id": identity, "session_id": session_id, "run_id": run_id,
                               "state": "pending", "revision": 1, **content, "continuation": continuation})

    def answer(self, identity: str, revision: int, answer: str) -> dict[str, Any]:
        with self.lock:
            item = self.get(identity)
            if item is None:
                raise ValueError("question not found")
            if item["state"] in {"answer_accepted", "answered"} and item.get("answer") == answer:
                return item
            if item["state"] != "pending":
                raise ValueError("question already answered or cancelled")
            if item["revision"] != revision:
                raise ValueError("question revision conflict")
            if not isinstance(answer, str) or not 0 < len(answer.strip()) <= 8000 or (
                not item["allow_free_text"] and answer not in item["options"]
            ):
                raise ValueError("invalid question answer")
            item.update(state="answer_accepted", revision=revision + 1, answer=answer)
            return self._save(item)

    def queued(self, identity: str) -> dict[str, Any]:
        with self.lock:
            item = self.get(identity)
            if item is None or item["state"] not in {"answer_accepted", "answered"}:
                raise ValueError("answer not accepted")
            if item["state"] == "answer_accepted":
                item.update(state="answered", revision=item["revision"] + 1)
                self._save(item)
            return item

    def cancel(self, identity: str, revision: int) -> dict[str, Any]:
        with self.lock:
            item = self.get(identity)
            if item is None or item["state"] != "pending" or item["revision"] != revision:
                raise ValueError("question is no longer pending")
            item.update(state="cancelled", revision=revision + 1)
            return self._save(item)

    def answer_input(self, item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        key = hashlib.sha256(item["id"].encode()).hexdigest()[:32]
        return "answer_" + key, {**item["continuation"], "session_id": item["session_id"], "run_id": "run_" + key,
            "task": item["answer"], "input": {"kind": "resume_run", "source_run_id": item["run_id"],
                "instruction": f"The user answered this question: {item['question']}\nAnswer: {item['answer']}\nContinue the original task within its existing authorization. Command execution still requires its own approval."}}
