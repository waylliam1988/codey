"""Bounded deterministic progress observations for every kernel task kind."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from codey.agents.runaway_guard import attempt_record, should_block_or_remind
from codey.agents.state import SeenInfoLRU, ToolAttemptRecord, seen_info_key
from codey.knowledge.model import canonical_json
from codey.operations.progress_comparison import EFFECT_OBSERVATIONS, ProjectCheckComparison, progress_comparison
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.progress import ProgressSnapshot
from codey.types.json_value import JsonObject
from codey.utils.refs import stable_ref


class ProgressSession(Protocol):
    @property
    def turn(self) -> int: ...

    @property
    def edited_files(self) -> Mapping[str, int]: ...

    @property
    def _memory_results(self) -> Mapping[str, ToolResult]: ...


@dataclass(frozen=True)
class ProjectCheckCycle:
    key: str
    feedback: str


def project_check_cycle(
    delivered: Sequence[ToolResult], history: Mapping[str, ToolResult],
    compare: Callable[[ToolResult], ProjectCheckComparison | None],
) -> ProjectCheckCycle | None:
    """Describe a repeated verified failure after an edit that changed no bytes."""
    recent = tuple(history.values())[-16:]
    for current in reversed(delivered):
        latest = compare(current)
        if latest is None or not latest.failure_ids:
            continue
        current_position = next((i for i, row in enumerate(recent) if row is current), None)
        if current_position is None:
            continue
        no_ops: set[str] = set()
        for previous in reversed(recent[:current_position]):
            if previous.call.name == "edit":
                if previous.audit.get("changed") is True:
                    break
                if previous.ok and previous.audit.get("changed") is False and isinstance(
                    path := previous.call.args.get("path"), str
                ):
                    no_ops.add(path)
            comparison = compare(previous)
            if comparison is None or comparison.execution_ref == latest.execution_ref:
                continue
            if comparison.key == latest.key and no_ops:
                failures = ", ".join(latest.failure_ids[:5])
                output = canonical_json({"tool": "task_output", "args": {"task_ref": latest.task_ref}})
                return ProjectCheckCycle(stable_ref("project_check", latest.key, *sorted(no_ops)),
                        f"The recent edit left {', '.join(sorted(no_ops))} unchanged, and the same project check "
                        f"reported the same failures on the same workspace. Failures: {failures[:1000]}. "
                        f"Execution receipt: {latest.execution_ref}. To read its original output use {output}. "
                        "Find what produces the recorded failure before changing the file or rerunning the check.")
            break
    return None


class KernelProgress:
    def __init__(self, stagnant_turns: int | None = None, *, snapshot: ProgressSnapshot | None = None) -> None:
        requested_limit = max(1, stagnant_turns) if stagnant_turns is not None else 4
        snapshot = snapshot or ProgressSnapshot(limit=requested_limit)
        if snapshot.last_batch_id and requested_limit != snapshot.limit:
            raise ValueError("progress budget differs from the admitted operation")
        self.limit = snapshot.limit if snapshot.last_batch_id else requested_limit
        self.seen = SeenInfoLRU()
        for key in snapshot.seen:
            self.seen.add(key)
        self.attempts: deque[ToolAttemptRecord] = deque(snapshot.attempts, maxlen=16)
        self.idle = snapshot.idle
        self.last_batch_id = snapshot.last_batch_id
        self.schema_version = snapshot.schema_version
        self.repair_failure_key = snapshot.repair_failure_key
        self.repair_rejections = snapshot.repair_rejections
        self.warned_check_cycle = snapshot.warned_check_cycle
        self.warning_batch_id = snapshot.warning_batch_id
        self.check_cycle_repeated = snapshot.check_cycle_repeated

    def snapshot(self) -> ProgressSnapshot:
        return ProgressSnapshot(self.limit, self.seen.keys(), tuple(self.attempts), self.idle, self.last_batch_id,
                                self.schema_version, self.repair_failure_key, self.repair_rejections,
                                self.warned_check_cycle, self.warning_batch_id, self.check_cycle_repeated)

    @property
    def stopped(self) -> bool:
        if self.repair_rejections >= self.limit or self.check_cycle_repeated:
            return True
        if self.warning_batch_id and (self.last_batch_id == self.warning_batch_id
                or (self.attempts and self.attempts[-1].tool in {"run", "task_output", "edit"})):
            return False
        # Proven reload cycles may stop as incomplete; effect delivery is not
        # permission to claim success, nor a reason to run until max_turns.
        reloads: list[ToolAttemptRecord] = []
        for record in reversed(self.attempts):
            if record.tool not in {"browser_navigate", "browser_snapshot"}:
                break
            reloads.append(record)
        if self.idle >= self.limit and any(record.tool == "browser_navigate" for record in reloads):
            return True
        safe: list[ToolAttemptRecord] = []
        for record in reversed(self.attempts):
            if record.tool in EFFECT_OBSERVATIONS:
                break
            safe.insert(0, record)
        # Once an effect has left the bounded history, all retained attempts
        # are observations again. Large admitted budgets still use full idle.
        idle = min(self.idle, len(safe)) if len(safe) < len(self.attempts) else self.idle
        return idle > 0 and (idle >= self.limit or should_block_or_remind(safe).block)

    def repeat_feedback(self) -> str:
        """Give the model a correction before the existing repeat limit wins."""
        if not self.idle or self.stopped or len(self.attempts) < 2:
            return ""
        previous, latest = list(self.attempts)[-2:]
        # Read-only repeats already stop above. A detected cycle that remains
        # runnable contains an effect and needs a reminder rather than a stop.
        effect_cycle = should_block_or_remind(self.attempts).block
        if not effect_cycle and (previous.call_fp != latest.call_fp or previous.result_fp != latest.result_fp
                or previous.changed or latest.changed or previous.edit_epoch != latest.edit_epoch):
            return ""
        status = "already succeeded" if latest.ok else "already failed"
        if latest.tool in EFFECT_OBSERVATIONS or effect_cycle:
            return ("Repeated browser operations have not provided new visible information. This does not establish "
                    "whether an effect occurred. Observe the current page before deciding another effect; use an "
                    "available snapshot or read action. Continue with outstanding requirements when the recorded "
                    "observations suffice. Repeat an effect only when the task and current observations justify it.")
        reminder = (f"The {latest.tool} call {status}; you repeated the same arguments and received the same result. "
                "Do not call it again with identical arguments. Continue from the recorded result with the next "
                "required action, change the request to address the observed problem, or explain the specific blocker.")
        if latest.tool == "research_plan":
            reminder += " Recording a plan is not completed investigation. Repeating it is not progress."
        return reminder

    def _observe_check(self, fact: ProjectCheckComparison, result: ToolResult, *, epoch: int, turn: int) -> bool | None:
        fresh_execution = self.seen.add(seen_info_key("check_execution", fact.execution_ref, "settled"))
        if not fresh_execution and result.call.name == "task_output" and (
            "stdout_cursor" in result.call.args or "stderr_cursor" in result.call.args
        ):
            # A new output page can provide information about the same execution.
            return None
        semantic = canonical_json(fact.key)
        self.attempts.append(attempt_record(result.call, ToolResult(result.call, semantic, ok=fact.exit_code == 0),
                                            turn=turn, edit_epoch=epoch))
        return fresh_execution and self.seen.add(seen_info_key("project_check", str(epoch), semantic))

    def _commit_check_observation(self, *, batch_id: str, cycle: str, progress: bool) -> None:
        if batch_id and cycle:
            self.check_cycle_repeated = self.warned_check_cycle == cycle and self.warning_batch_id != batch_id
            if not self.check_cycle_repeated:
                self.warned_check_cycle, self.warning_batch_id = cycle, batch_id
            self.schema_version = 3
        elif progress:
            self.warned_check_cycle, self.warning_batch_id, self.check_cycle_repeated = "", "", False

    def observe(self, results: Sequence[ToolResult], session: ProgressSession, *, batch_id: str = "",
                check_facts: Mapping[int, ProjectCheckComparison] | None = None, check_cycle: str = "") -> bool:
        """Compare observations without treating repeated effects as safe to stop.

        Progress accounting never crashes the loop: a telemetry fault for one
        result counts as no-progress for that result (safe direction: the
        task may stop earlier, never spins forever).
        """
        if batch_id and batch_id == self.last_batch_id:
            return self.stopped
        progress = False
        epoch = max([0, *session.edited_files.values()])
        check_moved = False
        for index, result in enumerate(results):
            try:
                fact = check_facts.get(index) if check_facts is not None else None
                if fact is not None:
                    settled = self._observe_check(fact, result, epoch=epoch, turn=session.turn)
                    if settled is not None:
                        progress = progress or settled
                        check_moved = check_moved or settled
                        continue
                comparison = progress_comparison(result, session._memory_results)
                call = comparison.call
                record = attempt_record(call, comparison.result, turn=session.turn, edit_epoch=epoch)
            except Exception:
                continue
            self.attempts.append(record)
            if call.name == "run" and result.kind == "accepted":
                continue
            changed = result.audit.get("changed") is True
            check_moved = check_moved or changed
            key = seen_info_key(call.name, str(call.args.get("path") or call.args.get("url") or
                                             call.args.get("id") or ""), comparison.information)
            novel = self.seen.add(key)
            prior = next((item for item in reversed(list(self.attempts)[:-1]) if item.call_fp == record.call_fp), None)
            observed_transition = (not comparison.reloaded
                                   and (call.name.startswith("browser_") or comparison.browser_observation) and prior is not None
                                   and prior.result_fp != record.result_fp)
            if changed or (record.ok and (novel or observed_transition)):
                progress = True
        self.idle = 0 if progress else self.idle + 1
        self.last_batch_id = batch_id
        self._commit_check_observation(batch_id=batch_id, cycle=check_cycle, progress=check_moved)
        # A newly changed file or genuinely new output must never be stopped
        # merely because its call arguments appeared earlier in the session.
        return self.stopped

    def reject_completion(self, args: JsonObject, feedback: str, session: ProgressSession, *, failure_key: str = "") -> None:
        """Account for a rejected proposal without inventing a tool execution.

        Unkeyed proposals retain ordinary exact-repeat/cycle accounting. A
        keyed domain issue has a durable rejection budget independent of tool
        history. Only a different validation issue starts a new repair episode;
        reads, paraphrases and replacement selections do not reset its count.
        Persist this snapshot atomically with the control decision. It does not
        claim a result batch: last_batch_id retains its original tool receipt.
        """
        call = ToolCall("done", args)
        result = ToolResult(call, failure_key or feedback, ok=False)
        record = attempt_record(call, result, turn=session.turn, edit_epoch=max([0, *session.edited_files.values()]))
        repeats = sum(previous.tool == "done" and previous.result_fp == record.result_fp for previous in self.attempts)
        self.attempts.append(record)
        novel = self.seen.add(seen_info_key("done", record.call_fp, feedback))
        self.idle = 0 if novel else self.idle + 1
        if failure_key:
            previous = self.repair_rejections if self.repair_failure_key == failure_key else (
                repeats if self.schema_version == 1 else 0)
            self.repair_failure_key = failure_key
            self.repair_rejections = min(self.limit, previous + 1)
            self.schema_version = max(self.schema_version, 2)
            self.idle = max(self.idle, self.repair_rejections)
        else:
            self.repair_failure_key = ""
            self.repair_rejections = 0
