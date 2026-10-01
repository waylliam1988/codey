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
11. completion_truthful -- completed implies proof, bound verification
    identity, and ledger-contained strict-Research citations.
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

    def check_completion_truthful(self, view: dict) -> None:
        """Completed implies proof requirements match actual durable facts.

        The view carries test-side facts read from the real log, workspace,
        and ledger -- never the verdict's own claims. Verification must bind
        the current (revision, fingerprint) pair exactly; strict Research
        must cite only opened sources from a valid ledger with a valid
        report. Missing fields fail closed: absent information never counts
        as success, ``True`` never stands in for an int revision, and
        ``None == None`` never counts as a fingerprint match.
        """
        if not isinstance(view, dict):
            _fail("completion_truthful", self._prefix("completion view must be a mapping"))
        completed = view.get("completed")
        if type(completed) is not bool:
            _fail("completion_truthful", self._prefix(f"completed must be an exact bool, got {completed!r}"))
        if completed is False:
            return
        if view.get("proof_exists") is not True:
            _fail("completion_truthful", self._prefix("completed without a durable proof"))
        if view.get("required_checks_passed") is not True:
            _fail("completion_truthful", self._prefix("completed with failing required checks"))
        if "verification_required" not in view or type(view.get("verification_required")) is not bool:
            _fail("completion_truthful", self._prefix("completed without an explicit verification requirement"))
        if "strict_research" not in view or type(view.get("strict_research")) is not bool:
            _fail("completion_truthful", self._prefix("completed without an explicit research requirement"))
        if view.get("verification_required") is True:
            if view.get("verification_identity_valid") is not True:
                _fail("completion_truthful", self._prefix("verification identity is not valid"))
            ver_rev = view.get("verification_revision")
            ws_rev = view.get("workspace_revision")
            if type(ver_rev) is not int or type(ws_rev) is not int:
                _fail(
                    "completion_truthful",
                    self._prefix(f"verification revision must be exact int, got {ver_rev!r} vs {ws_rev!r}"),
                )
            try:
                from codey.workspace.revision import valid_workspace_revision
            except Exception:
                valid_workspace_revision = lambda v: v if type(v) is int and v >= 1 else 0  # noqa: E731
            if not valid_workspace_revision(ver_rev) or not valid_workspace_revision(ws_rev):
                _fail("completion_truthful", self._prefix("verification revision is not a valid workspace revision"))
            if int(ver_rev) != int(ws_rev):
                _fail(
                    "completion_truthful",
                    self._prefix(f"stale verification revision {ver_rev!r} != workspace {ws_rev!r}"),
                )
            ver_fp = view.get("verification_fingerprint")
            ws_fp = view.get("workspace_fingerprint")
            try:
                from codey.workspace.revision import valid_workspace_fingerprint
            except Exception:
                valid_workspace_fingerprint = lambda v: v if isinstance(v, str) and v.startswith("sha256:") else ""  # noqa: E731
            if not valid_workspace_fingerprint(ver_fp) or not valid_workspace_fingerprint(ws_fp):
                _fail("completion_truthful", self._prefix("verification fingerprint is not valid"))
            if str(ver_fp) != str(ws_fp):
                _fail("completion_truthful", self._prefix("verification fingerprint mismatch"))
        if view.get("strict_research") is True:
            if view.get("ledger_valid") is not True:
                _fail("completion_truthful", self._prefix("strict Research without a valid ledger"))
            if view.get("report_valid") is not True:
                _fail("completion_truthful", self._prefix("strict-Research report is invalid"))
            for key in ("opened_sources", "cited_sources", "cited_evidence_sources"):
                if key not in view or not isinstance(view.get(key), (list, tuple)):
                    _fail("completion_truthful", self._prefix(f"strict Research without {key}"))
            opened = {str(u) for u in (view.get("opened_sources") or ()) if str(u)}
            cited = {str(u) for u in (view.get("cited_sources") or ()) if str(u)}
            cited_evidence = {str(u) for u in (view.get("cited_evidence_sources") or ()) if str(u)}
            if not opened:
                _fail("completion_truthful", self._prefix("strict Research without opened sources"))
            if not cited <= opened:
                _fail("completion_truthful", self._prefix("cited sources escape opened sources"))
            if not cited_evidence <= opened:
                _fail("completion_truthful", self._prefix("cited evidence escapes opened sources"))

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
        completion_views: list[dict] | None = None,
    ) -> dict:
        """Run the fact-shaped checks over one canonical snapshot.

        An intent row plus its settlement row share an effect id by design,
        so the duplicate key is (kind, id): the same fact recorded twice.
        Completion truthfulness runs here too when callers supply views built
        from real logs, workspaces, and ledgers (see
        :func:`completion_view_from_gate`).
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
        views = completion_views
        if views is None:
            raw_views = facts.get("completion_views") if isinstance(facts, dict) else None
            if isinstance(raw_views, list):
                views = raw_views
        if views is not None:
            for view in views:
                self.check_completion_truthful(view)
        return facts


def completion_view_from_gate(
    *,
    session: object,
    evidence: object,
    verdict: object,
    research_ledger: object = None,
    done_text: str = "",
) -> dict:
    """Build a truthfulness view from real gate facts, never verdict claims.

    Reads the durable session verifications, the current evidence workspace,
    the verdict's proof existence, the task policy requirements, and the real
    ledger/report inputs. Verification identity and ledger containment are
    recomputed here so the oracle independently rechecks them instead of
    trusting a ``*_valid = True`` claim.
    """
    try:
        from codey.workspace.revision import valid_workspace_fingerprint, valid_workspace_revision
    except Exception:
        valid_workspace_revision = lambda v: v if type(v) is int and v >= 1 else 0  # noqa: E731
        valid_workspace_fingerprint = lambda v: v if isinstance(v, str) and v.startswith("sha256:") else ""  # noqa: E731

    complete = bool(getattr(verdict, "complete", False) is True)
    proof = getattr(verdict, "proof", None)
    proof_exists = proof is not None
    try:
        rows = list(getattr(proof, "checks", ()) or ()) if proof is not None else []
    except Exception:
        rows = []
    try:
        required_checks_passed = bool(complete and bool(rows) and all(
            str(getattr(row, "status", "")) == "pass" for row in rows
        ))
    except Exception:
        required_checks_passed = False
    try:
        edited = dict(getattr(session, "edited_files", {}) or {})
    except Exception:
        edited = {}
    verification_forbidden = getattr(session, "verification_forbidden", False) is True
    verification_required = bool(edited) and not verification_forbidden
    try:
        latest_edit = max(int(v) for v in edited.values()) if edited else None
    except (TypeError, ValueError):
        latest_edit = None
    try:
        verifs = list(getattr(session, "verifications", ()) or [])
    except Exception:
        verifs = []
    latest_ver: dict | None = None
    if latest_edit is not None:
        for item in verifs:
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("revision", -1)) == latest_edit:
                    latest_ver = item
            except (TypeError, ValueError):
                continue
    try:
        workspace_revision = getattr(evidence, "workspace_revision", 0)
        workspace_fingerprint = str(getattr(evidence, "workspace_fingerprint", "") or "")
    except Exception:
        workspace_revision, workspace_fingerprint = 0, ""
    if latest_ver is None:
        verification_revision: object = None
        verification_fingerprint: object = ""
        verification_identity_valid = False
    else:
        verification_revision = latest_ver.get("workspace_revision")
        verification_fingerprint = latest_ver.get("workspace_fingerprint", "")
        verification_identity_valid = bool(
            type(verification_revision) is int
            and type(workspace_revision) is int
            and bool(valid_workspace_revision(verification_revision))
            and bool(valid_workspace_revision(workspace_revision))
            and int(verification_revision) == int(workspace_revision)
            and bool(valid_workspace_fingerprint(str(verification_fingerprint or "")))
            and bool(valid_workspace_fingerprint(str(workspace_fingerprint or "")))
            and str(verification_fingerprint) == str(workspace_fingerprint)
        )
    policy = getattr(session, "policy", None)
    strict_research = getattr(policy, "strict_research", False) is True
    ledger_valid = False
    opened_sources: list[str] = []
    if strict_research:
        try:
            finals = set(research_ledger.final_url_set()) if research_ledger is not None else set()
        except Exception:
            finals = set()
        opened_sources = sorted(str(u) for u in finals if str(u))
        ledger_valid = bool(opened_sources)
    try:
        evidence_rows = list(getattr(session, "evidence", []) or [])
    except Exception:
        evidence_rows = []
    cited_sources = sorted({
        str(row.get("source_url", "")) for row in evidence_rows
        if isinstance(row, dict) and str(row.get("source_url", ""))
    })
    cited_evidence_sources = list(cited_sources)
    if strict_research and research_ledger is not None:
        try:
            ledger_urls = {
                str(getattr(item, "source_url", "")) for item in (getattr(research_ledger, "evidence_items", ()) or ())
                if str(getattr(item, "source_url", ""))
            }
            cited_evidence_sources = sorted({u for u in cited_sources if u in ledger_urls} or ledger_urls)
        except Exception:
            cited_evidence_sources = list(cited_sources)
    text = str(done_text or "")
    report_valid = bool("结论" in text and "来源" in text) if strict_research else False
    if not strict_research:
        opened_sources = []
        cited_sources = []
        cited_evidence_sources = []
    return {
        "completed": complete,
        "proof_exists": proof_exists,
        "required_checks_passed": bool(required_checks_passed),
        "verification_required": bool(verification_required),
        "verification_identity_valid": bool(verification_identity_valid),
        "verification_revision": verification_revision,
        "workspace_revision": workspace_revision,
        "verification_fingerprint": verification_fingerprint,
        "workspace_fingerprint": workspace_fingerprint,
        "strict_research": bool(strict_research),
        "ledger_valid": bool(ledger_valid),
        "opened_sources": list(opened_sources),
        "cited_sources": list(cited_sources),
        "cited_evidence_sources": list(cited_evidence_sources),
        "report_valid": bool(report_valid),
    }


__all__ = ["InvariantChecker", "InvariantViolation", "completion_view_from_gate"]
