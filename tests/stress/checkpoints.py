"""checkpoints.py: the systematic crash-point matrix (Durability Lab Level 3).

Every durable writer x every internal crash point, with an honest status
per cell:

- ``covered``: a real ``Popen.kill()`` test kills at (a construction of)
  that point and recovery is oracle-checked. Names the test method.
- ``vacuous``: indistinguishable under real process kill. The page cache
  survives the process, so ``after_flush`` vs ``after_fsync`` (or anything
  between fsync and close) cannot be told apart without power loss, which
  no test here simulates. Claiming coverage would be theater.
- ``same``: behaviorally identical to another covered cell (e.g. a torn
  tmp file and a complete tmp file both leave the original intact behind
  an orphan tmp): asserted by the same test, noted here instead of
  duplicating spawns.

``test_crash_coverage.py`` keeps this table honest: every ``covered`` cell
must name a test method that actually exists.
"""

from __future__ import annotations

WRITERS = (
    "session-log-append",
    "ghost-append",
    "journal-append",
    "atomic-rewrite",
)

CRASH_POINTS = (
    "before_open",
    "after_open",
    "after_write",
    "after_flush",
    "after_fsync",
    "before_rename",
    "after_rename",
    "before_close",
    "after_close",
)

COVERED = "covered"
VACUOUS = "vacuous"
SAME = "same"

# (writer, crash point) -> (status, test method or reason).
COVERAGE: dict[tuple[str, str], tuple[str, str]] = {
    ("session-log-append", "before_open"): (
        COVERED, "test_real_kill_recovers_pending_provider_intent",
    ),
    ("session-log-append", "after_open"): (
        SAME, "no bytes yet: identical to before_open",
    ),
    ("session-log-append", "after_write"): (
        COVERED, "test_session_torn_row_is_repaired_and_baseline_survives",
    ),
    ("session-log-append", "after_flush"): (VACUOUS, "page cache survives process kill"),
    ("session-log-append", "after_fsync"): (
        COVERED, "test_session_full_write_without_ack_stays_durable",
    ),
    ("session-log-append", "before_rename"): (VACUOUS, "append path has no rename"),
    ("session-log-append", "after_rename"): (VACUOUS, "append path has no rename"),
    ("session-log-append", "before_close"): (VACUOUS, "close follows fsync; same as after_fsync"),
    ("session-log-append", "after_close"): (
        SAME, "returned write: identical to after_fsync",
    ),
    ("ghost-append", "before_open"): (
        COVERED, "test_real_kill_recovers_ghost_and_tool_batch",
    ),
    ("ghost-append", "after_open"): (
        SAME, "no bytes yet: identical to before_open",
    ),
    ("ghost-append", "after_write"): (
        COVERED, "test_ghost_partial_batch_leaves_exact_prefix",
    ),
    ("ghost-append", "after_flush"): (VACUOUS, "page cache survives process kill"),
    ("ghost-append", "after_fsync"): (
        SAME, "complete batch then kill: idle checkpoint, covered by ghost proc-kill test",
    ),
    ("ghost-append", "before_rename"): (VACUOUS, "append path has no rename"),
    ("ghost-append", "after_rename"): (VACUOUS, "append path has no rename"),
    ("ghost-append", "before_close"): (VACUOUS, "close follows fsync; same as after_fsync"),
    ("ghost-append", "after_close"): (
        SAME, "returned write: identical to after_fsync",
    ),
    ("journal-append", "before_open"): (
        COVERED, "test_real_kill_recovers_pending_provider_intent",
    ),
    ("journal-append", "after_open"): (
        SAME, "no bytes yet: identical to before_open",
    ),
    ("journal-append", "after_write"): (
        COVERED, "test_journal_torn_tail_does_not_break_recovery",
    ),
    ("journal-append", "after_flush"): (
        SAME, "journal has no fsync: flushed tail is durable absence-or-prefix, same assertions",
    ),
    ("journal-append", "after_fsync"): (VACUOUS, "journal performs no fsync by design"),
    ("journal-append", "before_rename"): (VACUOUS, "append path has no rename"),
    ("journal-append", "after_rename"): (VACUOUS, "append path has no rename"),
    ("journal-append", "before_close"): (
        SAME, "unflushed tail can only vanish, never corrupt: reader skips bad lines",
    ),
    ("journal-append", "after_close"): (
        SAME, "returned write is page-cache durable under process kill",
    ),
    ("atomic-rewrite", "before_open"): (
        COVERED, "test_real_kill_recovers_pending_provider_intent",
    ),
    ("atomic-rewrite", "after_open"): (
        SAME, "empty tmp, original intact: identical to before_open",
    ),
    ("atomic-rewrite", "after_write"): (
        SAME, "torn or complete tmp never replaces: asserted by the pre-rename test",
    ),
    ("atomic-rewrite", "after_flush"): (
        SAME, "tmp completeness changes nothing: asserted by the pre-rename test",
    ),
    ("atomic-rewrite", "after_fsync"): (
        COVERED, "test_primitive_replace_pre_rename_keeps_original_and_ignores_orphan_tmp",
    ),
    ("atomic-rewrite", "before_rename"): (
        COVERED, "test_primitive_replace_pre_rename_keeps_original_and_ignores_orphan_tmp",
    ),
    ("atomic-rewrite", "after_rename"): (
        COVERED, "test_primitive_replace_post_rename_keeps_new_content",
    ),
    ("atomic-rewrite", "before_close"): (VACUOUS, "rewrite returns after replace; no close step"),
    ("atomic-rewrite", "after_close"): (
        SAME, "returned rewrite: identical to after_rename",
    ),
}


def coverage_report() -> str:
    lines = ["writer x crash point -> status (test or reason)"]
    for writer in WRITERS:
        for point in CRASH_POINTS:
            status, detail = COVERAGE[(writer, point)]
            lines.append(f"  {writer:<20} {point:<14} {status:<8} {detail}")
    covered = sum(1 for status, _ in COVERAGE.values() if status == COVERED)
    total = len(COVERAGE)
    lines.append(f"covered cells: {covered}/{total} (rest vacuous or same-by-construction)")
    return "\n".join(lines)


__all__ = [
    "COVERAGE",
    "COVERED",
    "CRASH_POINTS",
    "SAME",
    "VACUOUS",
    "WRITERS",
    "coverage_report",
]
