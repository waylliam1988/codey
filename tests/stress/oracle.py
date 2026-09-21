"""InvariantChecker: the shared definition of "correct" for every stress test.

Unit, integration, stress, fault, and soak tests all end here. Each check
takes canonical facts (or the small transition that produced them) and
raises AssertionError with a replayable description on violation:

1. no_duplicate_facts -- one logical effect, at most one committed fact.
2. recovery_idempotent -- R(R(S)) == R(S).
3. replay_idempotent -- Replay(Replay(events)) == Replay(events).
4. projection_rebuildable -- incremental projection == from-scratch rebuild.
5. no_fake_success -- timeout with execution stays unknown, never success.
6. stop_allow_linearized -- Stop-before-commit means zero spawns, and a
   spawn never lands after a completed Stop.
7. completion_has_proof -- completed implies a durable proof exists.
8. no_new_operations_on_restart -- operation ids are stable across restart.
9. ghost_stable_across_restart -- ghost rows are identical before/after restart.
10. model_matches_durable -- scheduler memory agrees with durable reads.
"""

from __future__ import annotations

from collections.abc import Callable


class InvariantViolation(AssertionError):
    """One broken durable invariant, with enough detail to replay it."""


def _fail(name: str, detail: str) -> None:
    raise InvariantViolation(f"[{name}] {detail}")


class InvariantChecker:
    def __init__(self, seed: int | None = None) -> None:
        self.seed = seed

    def _prefix(self, detail: str) -> str:
        return f"{detail} (seed={self.seed})" if self.seed is not None else detail

    def check_no_duplicate_facts(self, committed_ids: list[str]) -> None:
        """One logical effect id may commit at most one fact row."""
        seen: set[str] = set()
        for effect_id in committed_ids:
            if effect_id in seen:
                _fail("no_duplicate_facts", self._prefix(f"duplicate fact for {effect_id!r}"))
            seen.add(effect_id)

    def check_recovery_idempotent(self, recover: Callable[[], dict]) -> dict:
        """Recovering twice must equal recovering once."""
        first = recover()
        second = recover()
        if first != second:
            _fail(
                "recovery_idempotent",
                self._prefix(f"R(R(S)) != R(S):\nfirst={first!r}\nsecond={second!r}"),
            )
        return first

    def check_replay_idempotent(
        self, fold: Callable[[tuple], tuple], rows: tuple, replayed: tuple
    ) -> None:
        """Re-applying the same rows must not change the fold."""
        once = fold(rows)
        twice = fold(rows + replayed)
        if once != twice:
            _fail(
                "replay_idempotent",
                self._prefix(f"fold changed under replay:\nonce={once!r}\ntwice={twice!r}"),
            )

    def check_projection_rebuildable(self, incremental: object, rebuilt: object) -> None:
        if incremental != rebuilt:
            _fail(
                "projection_rebuildable",
                self._prefix(f"incremental != rebuild:\n{incremental!r}\n{rebuilt!r}"),
            )

    def check_no_fake_success(self, unknowns: list[tuple[str, str]]) -> None:
        """(op_id, status) pairs that timed out with execution must never read success."""
        for operation_id, status in unknowns:
            if status in {"success", "ok", "settled-ok"}:
                _fail(
                    "no_fake_success",
                    self._prefix(f"unknown outcome of {operation_id!r} disguised as {status!r}"),
                )

    def check_stop_allow_linearized(
        self, spawns: list[float], stop_done_at: float | None
    ) -> None:
        """No spawn may land after a completed Stop; spawns are at most one per ticket."""
        if len(spawns) > 1:
            _fail(
                "stop_allow_linearized",
                self._prefix(f"ticket spawned {len(spawns)} times, want at most 1"),
            )
        if stop_done_at is not None:
            for at in spawns:
                if at >= stop_done_at:
                    _fail(
                        "stop_allow_linearized",
                        self._prefix(f"spawn at {at} landed after Stop completed at {stop_done_at}"),
                    )

    def check_completion_has_proof(self, completions: list[tuple[str, bool]]) -> None:
        """(operation_id, has_proof) pairs: completed implies proof exists."""
        for operation_id, has_proof in completions:
            if not has_proof:
                _fail(
                    "completion_has_proof",
                    self._prefix(f"completed {operation_id!r} has no durable proof"),
                )

    def check_no_new_operations_on_restart(
        self, before: list[str], after: list[str]
    ) -> None:
        """Restart must not invent logical operations."""
        if sorted(before) != sorted(after):
            _fail(
                "no_new_operations_on_restart",
                self._prefix(f"operation ids changed:\nbefore={sorted(before)!r}\nafter={sorted(after)!r}"),
            )

    def check_ghost_stable_across_restart(
        self, before: list[str], after: list[str]
    ) -> None:
        """The ghost log is append-only: a restart must rebuild the same rows."""
        if before != after:
            _fail(
                "ghost_stable_across_restart",
                self._prefix(
                    f"ghost rows changed across restart:\nbefore={before!r}\nafter={after!r}"
                ),
            )

    def check_model_matches_durable(
        self, surface: str, model_ids: list[str], durable_ids: list[str]
    ) -> None:
        """The scheduler's lifecycle model must agree with durable reads.

        The scheduler is harness memory; the world is bytes on disk. If they
        ever disagree, one of them is wrong -- that is exactly the class of
        bug where the harness becomes a second Runtime.
        """
        if sorted(model_ids) != sorted(durable_ids):
            _fail(
                "model_matches_durable",
                self._prefix(
                    f"{surface} model != durable:\nmodel={sorted(model_ids)!r}\n"
                    f"durable={sorted(durable_ids)!r}"
                ),
            )

    def assert_valid(
        self,
        facts: dict,
        *,
        unknowns: list[tuple[str, str]] | None = None,
        completions: list[tuple[str, bool]] | None = None,
    ) -> dict:
        """Run the fact-shaped checks over one canonical snapshot.

        An intent row plus its settlement row share an effect id by design,
        so the duplicate key is (kind, id): the same fact recorded twice.
        """
        committed = []
        for row in list(facts.get("log_rows", [])) + list(facts.get("ghost_rows", [])):
            key = str(row.get("effect_id") or row.get("batch_id") or row.get("id") or "")
            if key:
                committed.append(f"{row.get('kind', '')}/{row.get('record', '')}:{key}")
        self.check_no_duplicate_facts(committed)
        if unknowns is not None:
            self.check_no_fake_success(unknowns)
        if completions is not None:
            self.check_completion_has_proof(completions)
        return facts


__all__ = ["InvariantChecker", "InvariantViolation"]
