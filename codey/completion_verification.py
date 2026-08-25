"""Pure coding verification facts and completion-proof projection (0.4.13).

TaskRunner collects execution facts and wires I/O; this module explains what
those facts mean for completion. It is a projection leaf like
``completion_contract``: no I/O, no models, no commands, and it never treats
a model's own claim as a local fact.

Three vocabularies live here:

- Tri-state freshness (0.4.9): ``fresh_pass`` / ``fresh_fail`` /
  ``unobserved`` over locally observed checks that cover the selected
  verification candidate.
- Explicit provenance (0.4.13 prerequisite): the legacy ``checks_passed``
  bool inherited claims from checkpoints and review repairs invisibly.
  Provenance now says exactly where a green fact came from --
  ``local_run`` / ``checkpoint`` / ``none`` -- and an inherited pass is a
  limitation, never this round's clean verification fact.
- Deterministic failure classification: ``product_failure`` /
  ``environment_failure`` / ``verification_unavailable`` /
  ``provider_failure`` / ``unknown``. Classification is rule-based on
  observed exit/error codes plus a closed vocabulary of line-anchored
  output signatures that name the execution environment (missing
  dependency or tool, network failure, test-infra crash); it is not a
  critic and never diagnoses.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from codey.completion_contract import (
    CHECK_FAIL,
    CHECK_NOT_APPLICABLE,
    CHECK_NOT_RUN,
    CHECK_PASS,
    DOMAIN_CODING,
    CompletionCheck,
    CompletionProof,
    build_completion_contract,
    completion_check,
    project_completion_proof,
    safe_run_ref,
)
from codey.execution_evidence import CheckEvidence, ExecutionEvidence
from codey.research.evidence_runtime import normalize_runtime_ref
from codey.verification_policy import (
    check_covers_selected_candidate,
    is_document_path,
)


CODING_CHECK_RELEVANT_VERIFICATION = "relevant_verification"
LIMITATION_DOCS_ONLY_CHANGE = "docs_only_change"
LIMITATION_VERIFICATION_NOT_LOCALLY_OBSERVED = "verification_not_locally_observed"
LIMITATION_INHERITED_VERIFICATION = "inherited_verification_not_fresh"

# Tri-state local verification truth. "Some tool event happened" never proves
# a relevant check ran: reads and searches are events too, so a run that only
# edited and browsed stays unobserved instead of being misread as a failure.
VERIFICATION_FRESH_PASS = "fresh_pass"
VERIFICATION_FRESH_FAIL = "fresh_fail"
VERIFICATION_UNOBSERVED = "unobserved"

# Provenance stance of the green/red fact behind a proof. Only
# fresh_pass + local_run is a clean verification fact; an inherited pass
# keeps the receipt green but marks the proof complete_with_limitations.
STANCE_FRESH_PASS = VERIFICATION_FRESH_PASS
STANCE_FRESH_FAIL = VERIFICATION_FRESH_FAIL
STANCE_INHERITED_PASS = "inherited_pass"
STANCE_UNVERIFIED = "unverified"
PROVENANCE_STANCES = frozenset({
    STANCE_FRESH_PASS,
    STANCE_FRESH_FAIL,
    STANCE_INHERITED_PASS,
    STANCE_UNVERIFIED,
})

SOURCE_LOCAL_RUN = "local_run"
SOURCE_CHECKPOINT = "checkpoint"
SOURCE_NONE = "none"
PROVENANCE_SOURCES = frozenset({SOURCE_LOCAL_RUN, SOURCE_CHECKPOINT, SOURCE_NONE})

# Deterministic failure classes. Only product_failure is a repair candidate;
# everything else means "stop honestly", never "fix the code".
FAILURE_PRODUCT = "product_failure"
FAILURE_ENVIRONMENT = "environment_failure"
FAILURE_VERIFICATION_UNAVAILABLE = "verification_unavailable"
FAILURE_PROVIDER = "provider_failure"
FAILURE_UNKNOWN = "unknown"
FAILURE_CLASSES = frozenset({
    FAILURE_PRODUCT,
    FAILURE_ENVIRONMENT,
    FAILURE_VERIFICATION_UNAVAILABLE,
    FAILURE_PROVIDER,
    FAILURE_UNKNOWN,
})

# Closed vocabulary of observed-output signatures whose non-zero exit names
# the execution environment -- not the changed code: a missing interpreter
# dependency or tool, a network-dependent test, or a crashed test runner.
# Matching is line-anchored (see ``environment_failure_signal``): a
# signature only counts when it begins its diagnostic line, so a product
# assertion that merely quotes these words never matches.
ENVIRONMENT_FAILURE_SIGNATURES = (
    # missing python dependency / module
    "modulenotfounderror",
    "no module named",
    "importerror: dll load failed",
    # missing executable or node module
    "command not found",
    "is not recognized as an internal or external command",
    "cannot find module",
    "err_module_not_found",
    # missing package / unresolvable install
    "no matching distribution found",
    "could not find a version that satisfies",
    # network-dependent tests and downloads
    "could not resolve host",
    "temporary failure in name resolution",
    "connection refused",
    "connection reset by peer",
    "etimedout",
    "econnrefused",
    "enotfound",
    # test-infrastructure crashes
    "internalerror",
    "segmentation fault",
    "core dumped",
)

# Runner banners that may precede a diagnostic line, plus the structured
# heads they are stripped past. Assertion scaffolding such as an
# ``AssertionError:`` or ``Failed:`` prefix is deliberately absent -- those
# heads are CamelCase exception names, and the tool-head rules below only
# ever strip lowercase program names or quoted commands, so a diff line
# quoting one of the signatures must stay unmatched.
_DIAGNOSTIC_LINE_PREFIXES = (
    "e ",  # pytest marks failure context with E
    "> ",  # pytest echoes the failing source line
    "error: ",  # runners and package managers banner their diagnostics
    "fatal: ",
    "npm err! ",
    "/bin/bash: ",  # shells announce their own errors by name
    "/bin/sh: ",
    "bash: ",
    "zsh: ",
    "sh: ",
)

_SHELL_TRACE_PREFIX = re.compile(r"[^:\s][^:]*:\s*line\s+\d+:\s*", re.IGNORECASE)
_LOWERCASE_TOOL_HEAD = re.compile(r"[a-z0-9_][a-z0-9_./\\-]*:\s+")
_QUOTED_COMMAND_HEAD = re.compile(r"'[^']+'\s+")


def _strip_diagnostic_head(line: str) -> str:
    """Strip runner banners and tool-name heads from one raw output line."""

    while True:
        folded = line.casefold()
        banner = next(
            (
                prefix
                for prefix in _DIAGNOSTIC_LINE_PREFIXES
                if folded.startswith(prefix)
            ),
            None,
        )
        if banner is not None:
            line = line[len(banner):].lstrip()
            continue
        head = next(
            (
                match
                for match in (
                    _SHELL_TRACE_PREFIX.match(line),
                    _LOWERCASE_TOOL_HEAD.match(line),
                    _QUOTED_COMMAND_HEAD.match(line),
                )
                if match is not None
            ),
            None,
        )
        if head is None:
            return line.strip()
        line = line[head.end():].lstrip()


def _diagnostic_lines(*texts: object) -> Iterator[str]:
    """Fold observed output into stripped, casefolded diagnostic lines."""

    for raw_line in "\n".join(str(text or "") for text in texts).splitlines():
        line = _strip_diagnostic_head(raw_line.strip())
        if line:
            yield line.casefold()


def environment_failure_signal(*texts: object) -> bool:
    """Whether an observed diagnostic line names the environment as cause.

    Anchored matching: a signature counts only when it begins a diagnostic
    line once runner banners and tool-name heads are stripped. Real runner
    output prints these signatures as their own diagnostic; a product
    assertion quoting the same words mid-sentence --
    ``E   AssertionError: cannot find module`` -- never matches, so a
    fixable failure is never misread as an environment problem.
    """

    return any(
        line.startswith(signature)
        for line in _diagnostic_lines(*texts)
        for signature in ENVIRONMENT_FAILURE_SIGNATURES
    )


@dataclass(frozen=True)
class VerificationProvenance:
    """Where the decisive green/red fact for this round came from."""

    stance: str
    source: str

    @property
    def observed(self) -> bool:
        return self.stance in (STANCE_FRESH_PASS, STANCE_FRESH_FAIL)

    @property
    def clean_verification(self) -> bool:
        return self.stance == STANCE_FRESH_PASS and self.source == SOURCE_LOCAL_RUN

    def to_payload(self) -> dict[str, str]:
        return {"stance": self.stance, "source": self.source}


def coding_verification_state(
    selected_check: object,
    evidence: ExecutionEvidence,
    files: tuple[str, ...],
) -> str:
    """Classify local verification freshness for the selected candidate.

    A relevant check that failed after the latest edit wins over any that
    passed: the hard gate reports the worst locally observed fact.
    """

    if selected_check is None:
        return VERIFICATION_UNOBSERVED
    if any(
        check_covers_selected_candidate(selected_check, item.command, item.cwd, files)
        for item in evidence.failed_checks_after_edit
    ):
        return VERIFICATION_FRESH_FAIL
    if any(
        check_covers_selected_candidate(selected_check, item.command, item.cwd, files)
        for item in evidence.successful_checks
    ):
        return VERIFICATION_FRESH_PASS
    return VERIFICATION_UNOBSERVED


def verification_provenance(
    *,
    local_state: str,
    checkpoint_green: bool,
) -> VerificationProvenance:
    """Resolve explicit provenance from local facts plus boundary inheritance.

    Precedence: a locally observed fail beats a locally observed pass; both
    beat any inherited green. An inherited pass (checkpoint resume or the
    narrow pre-review green rule) stays green but is marked as not-fresh so
    it can never satisfy a clean-completion claim again.
    """

    if local_state == VERIFICATION_FRESH_FAIL:
        return VerificationProvenance(STANCE_FRESH_FAIL, SOURCE_LOCAL_RUN)
    if local_state == VERIFICATION_FRESH_PASS:
        return VerificationProvenance(STANCE_FRESH_PASS, SOURCE_LOCAL_RUN)
    if checkpoint_green:
        return VerificationProvenance(STANCE_INHERITED_PASS, SOURCE_CHECKPOINT)
    return VerificationProvenance(STANCE_UNVERIFIED, SOURCE_NONE)


def relevant_verification_pairs(
    verification_state: str,
    selected_check: object,
    evidence: ExecutionEvidence,
    files: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """The check commands that actually decided the verification state.

    Provenance cites decisive facts only: a fresh-pass proof cites covering
    passing commands, a fresh-fail proof cites covering failing commands,
    and an unobserved state cites nothing -- unrelated executed commands
    stay out of the proof.
    """

    if selected_check is None:
        return ()
    if verification_state == VERIFICATION_FRESH_FAIL:
        items = evidence.failed_checks_after_edit
    elif verification_state == VERIFICATION_FRESH_PASS:
        items = evidence.successful_checks
    else:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in items:
        if not check_covers_selected_candidate(selected_check, item.command, item.cwd, files):
            continue
        pair = (item.command, item.cwd)
        if pair not in pairs:
            pairs.append(pair)
    return tuple(pairs)


def _analysis_run_cwd_digest(cwd: object, project: object) -> str:
    from codey.research.identity import path_ref

    return str(path_ref(str(cwd or "."), project=project).get("digest") or "")


def matching_analysis_run_refs(
    analysis_runs: object,
    pairs: tuple[tuple[str, str], ...],
    project: object = None,
) -> tuple[str, ...]:
    """Latest analysis-run ref per decisive (command, cwd) pair.

    Matching is cwd-aware via the same project-relative path digest the
    AnalysisRun projection uses, so the same command run under two packages
    of a monorepo cites its own execution, never a sibling's. Redacted
    commands carry no display text and therefore no ref; their digest-only
    provenance stays in the analysis_runs trace section.
    """

    wanted: dict[tuple[str, str], None] = {}
    for command, cwd in dict.fromkeys(pairs or ()):
        text = str(command or "").strip()
        if not text:
            continue
        wanted[(text, _analysis_run_cwd_digest(cwd, project))] = None
    if not wanted:
        return ()
    by_pair: dict[tuple[str, str], str] = {}
    for row in analysis_runs or ():
        if not isinstance(row, Mapping):
            continue
        display = str(row.get("command_display") or "")
        ref = normalize_runtime_ref(row.get("analysis_run_id"), kind="analysis_run")
        cwd_ref = row.get("cwd_ref")
        digest = (
            str(cwd_ref.get("digest") or "")
            if isinstance(cwd_ref, Mapping)
            else ""
        )
        if not display or not ref or not digest:
            continue
        key = (display, digest)
        if key in wanted:
            by_pair[key] = ref
    refs: list[str] = []
    for key in wanted:
        ref = by_pair.get(key)
        if ref and ref not in refs:
            refs.append(ref)
    return tuple(refs)


def coding_completion_checks(
    *,
    files: tuple[str, ...],
    selected_check_present: bool,
    provenance: VerificationProvenance,
) -> tuple[CompletionCheck, ...]:
    """Project local coding facts into completion checks.

    The model's own claim never satisfies a check by itself, and a falsy
    reported value is not a failure fact either: without a locally observed
    result the honest projection is "could not verify" (not_run), never
    "verified bad" (fail). Failure is reserved for observed failures, and
    an inherited pass passes with a limitation instead of counting as
    this round's clean verification.
    """

    if files and all(is_document_path(str(item)) for item in files):
        row = completion_check(
            CODING_CHECK_RELEVANT_VERIFICATION,
            CHECK_NOT_APPLICABLE,
            LIMITATION_DOCS_ONLY_CHANGE,
        )
        return (row,) if row else ()
    if not selected_check_present:
        row = completion_check(
            CODING_CHECK_RELEVANT_VERIFICATION,
            CHECK_NOT_RUN,
            "no_matching_verification_command",
        )
        return (row,) if row else ()
    if provenance.stance == STANCE_FRESH_PASS:
        row = completion_check(CODING_CHECK_RELEVANT_VERIFICATION, CHECK_PASS)
    elif provenance.stance == STANCE_FRESH_FAIL:
        row = completion_check(
            CODING_CHECK_RELEVANT_VERIFICATION,
            CHECK_FAIL,
            "relevant_verification_failed",
        )
    elif provenance.stance == STANCE_INHERITED_PASS:
        row = completion_check(
            CODING_CHECK_RELEVANT_VERIFICATION,
            CHECK_PASS,
            LIMITATION_INHERITED_VERIFICATION,
        )
    else:
        row = completion_check(
            CODING_CHECK_RELEVANT_VERIFICATION,
            CHECK_NOT_RUN,
            LIMITATION_VERIFICATION_NOT_LOCALLY_OBSERVED,
        )
    return (row,) if row else ()


def coding_completion_limitations(
    *,
    files: tuple[str, ...],
    provenance: VerificationProvenance,
) -> tuple[str, ...]:
    """Limitation refs a satisfied-but-not-clean proof must carry."""

    if files and all(is_document_path(str(item)) for item in files):
        return (LIMITATION_DOCS_ONLY_CHANGE,)
    if provenance.stance == STANCE_INHERITED_PASS:
        return (LIMITATION_INHERITED_VERIFICATION,)
    return ()


def build_coding_completion_proof(
    *,
    run_id: str,
    stop_reason: str,
    task_changed: bool,
    files: tuple[str, ...],
    selected_check_present: bool,
    provenance: VerificationProvenance,
    analysis_run_refs: tuple[str, ...] = (),
) -> CompletionProof | None:
    """Project one coding run into its completion proof (or None)."""

    if stop_reason != "done" or not task_changed or not files:
        return None
    docs_only = all(is_document_path(str(item)) for item in files)
    if docs_only:
        limitations: tuple[str, ...] = (LIMITATION_DOCS_ONLY_CHANGE,)
    else:
        limitations = coding_completion_limitations(
            files=files,
            provenance=provenance,
        )
    run_ref = safe_run_ref(run_id)
    contract = build_completion_contract(
        domain=DOMAIN_CODING,
        subject_ref=f"run:{run_ref}" if run_ref else DOMAIN_CODING,
        checks=coding_completion_checks(
            files=files,
            selected_check_present=selected_check_present,
            provenance=provenance,
        ),
        limitation_refs=limitations,
        analysis_run_refs=analysis_run_refs,
        external_refs=(
            f"ledger:{run_ref}",
            f"receipt:{run_ref}",
            f"diff:{run_ref}",
        ) if run_ref else (),
    )
    return project_completion_proof(contract)


def classify_verification_failure(
    *,
    proof_status: str,
    selected_check_present: bool = True,
    decisive_error_code: str = "",
    decisive_exit_code: int | None = None,
    decisive_result_summary: str = "",
    provider_failed: bool = False,
) -> str:
    """Deterministically classify why completion did not hold.

    The rules are intentionally shallow: an observed non-zero exit is a
    product failure candidate, a tool-level error without an exit code
    means the check could not even execute (environment), an output tail
    whose diagnostic lines name the environment (missing dependency/tool,
    network, crashed test runner) is environment too -- never silently a
    code bug -- and anything unverifiable is unavailable.
    """

    if provider_failed:
        return FAILURE_PROVIDER
    if proof_status == "blocked":
        return FAILURE_VERIFICATION_UNAVAILABLE
    if proof_status == "failed":
        if not selected_check_present:
            return FAILURE_VERIFICATION_UNAVAILABLE
        error_code = str(decisive_error_code or "").strip()
        if error_code and decisive_exit_code is None:
            # The command never produced a process exit: approval denied,
            # timeout, spawn failure. That is not evidence the code broke.
            return FAILURE_ENVIRONMENT
        if environment_failure_signal(error_code, decisive_result_summary):
            # The process exited non-zero, but its own diagnostic lines
            # name the execution environment as the cause, not the edit.
            return FAILURE_ENVIRONMENT
        return FAILURE_PRODUCT
    return FAILURE_UNKNOWN


def decisive_failure_fact(
    selected_check: object,
    evidence: ExecutionEvidence,
    files: tuple[str, ...],
) -> CheckEvidence | None:
    """The first failing check that covers the selected candidate, if any."""

    if selected_check is None:
        return None
    for item in evidence.failed_checks_after_edit:
        if check_covers_selected_candidate(selected_check, item.command, item.cwd, files):
            return item
    return None


def repairable_failure_class(failure_class: str) -> bool:
    """Only observed product failures may become repair candidates."""

    return failure_class == FAILURE_PRODUCT


__all__ = [
    "CODING_CHECK_RELEVANT_VERIFICATION",
    "ENVIRONMENT_FAILURE_SIGNATURES",
    "FAILURE_CLASSES",
    "FAILURE_ENVIRONMENT",
    "FAILURE_PRODUCT",
    "FAILURE_PROVIDER",
    "FAILURE_UNKNOWN",
    "FAILURE_VERIFICATION_UNAVAILABLE",
    "LIMITATION_DOCS_ONLY_CHANGE",
    "LIMITATION_INHERITED_VERIFICATION",
    "LIMITATION_VERIFICATION_NOT_LOCALLY_OBSERVED",
    "PROVENANCE_SOURCES",
    "PROVENANCE_STANCES",
    "SOURCE_CHECKPOINT",
    "SOURCE_LOCAL_RUN",
    "SOURCE_NONE",
    "STANCE_FRESH_FAIL",
    "STANCE_FRESH_PASS",
    "STANCE_INHERITED_PASS",
    "STANCE_UNVERIFIED",
    "VERIFICATION_FRESH_FAIL",
    "VERIFICATION_FRESH_PASS",
    "VERIFICATION_UNOBSERVED",
    "VerificationProvenance",
    "build_coding_completion_proof",
    "classify_verification_failure",
    "coding_completion_checks",
    "coding_completion_limitations",
    "coding_verification_state",
    "decisive_failure_fact",
    "environment_failure_signal",
    "matching_analysis_run_refs",
    "relevant_verification_pairs",
    "repairable_failure_class",
    "verification_provenance",
]
