"""Deterministic edit-integrity observation for one coding run (0.5.0).

A green check proves the code passed; it does not prove the code was not
steered around. This monitor reads one run's changed paths and the unified
diff the change collection already produced, and asks a single question:
did this run weaken its own verification? Typical tampering -- deleting or
commenting a test import, wrapping it in ``except ImportError``, skipping a
test, thinning assertions, or narrowing verification config -- is visible
in the diff alone.

The observation is a bounded, refs-only projection: reason codes, severity,
affected paths, and content-addressed refs. Raw diff text is used inside
this module and never leaves it. The monitor produces findings, never a
completion verdict, and it is not evidence: nothing here enters the
EvidenceLedger, authorizes a tool, or changes a PermissionProfile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from codey.completion.edit_scope import (
    EDIT_SCOPE_FIXTURE,
    EDIT_SCOPE_GENERATED_VENDOR,
    EDIT_SCOPE_PRODUCTION,
    EDIT_SCOPE_TEST,
    EDIT_SCOPE_VERIFICATION_CONFIG,
    changed_paths_from_changes,
    classify_edit_path,
    task_authorizes_test_edit,
)
from codey.utils.refs import clip, digest_text, identifier, stable_ref


EDIT_INTEGRITY_SCHEMA_VERSION = 1

STATUS_CLEAN = "clean"
STATUS_SUSPICIOUS = "suspicious"
STATUS_UNOBSERVED = "unobserved"
STATUS_MONITOR_ERROR = "monitor_error"
EDIT_INTEGRITY_STATUSES = frozenset(
    {STATUS_CLEAN, STATUS_SUSPICIOUS, STATUS_UNOBSERVED, STATUS_MONITOR_ERROR}
)

SEVERITY_NONE = "none"
SEVERITY_LOW = "low"
SEVERITY_HIGH = "high"
SEVERITY_CRITICAL = "critical"
EDIT_INTEGRITY_SEVERITIES = frozenset(
    {SEVERITY_NONE, SEVERITY_LOW, SEVERITY_HIGH, SEVERITY_CRITICAL}
)
_SEVERITY_ORDER = {
    SEVERITY_NONE: 0,
    SEVERITY_LOW: 1,
    SEVERITY_HIGH: 2,
    SEVERITY_CRITICAL: 3,
}

REASON_TEST_IMPORT_REMOVED = "test_import_removed_or_commented"
REASON_TEST_IMPORT_GUARDED = "test_import_guarded"
REASON_TEST_SKIP_ADDED = "test_skip_added"
REASON_TEST_ASSERTIONS_REMOVED = "test_assertions_removed"
REASON_TEST_EXPECTED_EXCEPTION_WIDENED = "test_expected_exception_widened"
REASON_VERIFICATION_CONFIG_NARROWED = "verification_config_narrowed"
REASON_TEST_EDIT_WITHOUT_PRODUCTION_CHANGE = "test_edit_without_production_change"
REASON_UNAUTHORIZED_TEST_EDIT = "unauthorized_test_edit"
REASON_MONITOR_ERROR = "monitor_error"
EDIT_INTEGRITY_REASON_CODES = frozenset(
    {
        REASON_TEST_IMPORT_REMOVED,
        REASON_TEST_IMPORT_GUARDED,
        REASON_TEST_SKIP_ADDED,
        REASON_TEST_ASSERTIONS_REMOVED,
        REASON_TEST_EXPECTED_EXCEPTION_WIDENED,
        REASON_VERIFICATION_CONFIG_NARROWED,
        REASON_TEST_EDIT_WITHOUT_PRODUCTION_CHANGE,
        REASON_UNAUTHORIZED_TEST_EDIT,
        REASON_MONITOR_ERROR,
    }
)

MAX_FINDINGS = 8
MAX_AFFECTED_PATHS = 12
MAX_REASON_CODES = 8
MAX_SECTION_LINES = 2_000

_IMPORTED_MODULE_RE = re.compile(
    r"^(?:import\s+([.\w]+)|from\s+([.\w]+)\s+import\b)"
)
_TRY_GUARD_EXCEPT_RE = re.compile(
    r"^except\s+(?:\(?\s*)?(ImportError|ModuleNotFoundError)\b"
)
_SKIP_ADDED_RE = re.compile(
    r"pytest\.importorskip\s*\(|pytest\.skip\s*\(|@pytest\.mark\.skip\b|"
    r"@unittest\.skip\b|unittest\.skip(?:If|Unless)\s*\(",
    re.IGNORECASE,
)
_ASSERT_LINE_RE = re.compile(
    r"^\s*(?:assert\b|self\.assert(?:True|Equal|In|Is|IsNot|IsNone|IsNotNone|"
    r"Raises|AlmostEqual|Greater|Less)\b|(?:with\s+)?pytest\.raises\s*\()"
)
_RAISES_TARGET_RE = re.compile(r"pytest\.raises\s*\(\s*([A-Za-z_][\w.]*)")
_BENIGN_WIDE_TARGETS = frozenset({"Exception", "BaseException"})
_NARROWED_FLAGS = ("--deselect", "--ignore")
_NARROWED_K_RE = re.compile(r"""-k\s+"?not\b""")
_TESTPATHS_KEY_RE = re.compile(r"^\s*testpaths\b")
_TESTPATHS_VALUE_RE = re.compile(r"""["']([^"']+)["']""")

_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")


@dataclass(frozen=True)
class EditIntegrityFinding:
    """One bounded integrity finding: refs and codes, never diff text."""

    finding_ref: str
    reason_code: str
    severity: str
    paths: tuple[str, ...]
    summary: str

    def to_payload(self) -> dict[str, object]:
        return {
            "finding_ref": self.finding_ref,
            "reason_code": self.reason_code,
            "severity": self.severity,
            "paths": list(self.paths),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class EditIntegrityObservation:
    """The projected edit-integrity state of one coding run.

    ``status`` is the honest monitor outcome -- a monitor that could not
    observe (no changed paths) or failed outright is never ``clean``.
    ``diagnostic_refs`` are the refs a completion proof carries so the
    proof names the observation that qualifies it.
    """

    schema_version: int
    run_id: str
    status: str
    severity: str
    reason_codes: tuple[str, ...]
    findings: tuple[EditIntegrityFinding, ...]
    user_authorized_test_edit: bool
    affected_paths: tuple[str, ...]
    verification_refs: tuple[str, ...]
    change_refs: tuple[str, ...]
    observation_ref: str
    monitor_error_ref: str = ""

    @property
    def diagnostic_refs(self) -> tuple[str, ...]:
        if self.status in (STATUS_SUSPICIOUS, STATUS_MONITOR_ERROR) and self.observation_ref:
            return (self.observation_ref,)
        return ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "observation_ref": self.observation_ref,
            "status": self.status,
            "severity": self.severity,
        }
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.reason_codes:
            payload["reason_codes"] = list(self.reason_codes[:MAX_REASON_CODES])
        if self.findings:
            payload["findings"] = [
                finding.to_payload() for finding in self.findings[:MAX_FINDINGS]
            ]
        if self.user_authorized_test_edit:
            payload["user_authorized_test_edit"] = True
        if self.affected_paths:
            payload["affected_paths"] = list(self.affected_paths[:MAX_AFFECTED_PATHS])
        if self.verification_refs:
            payload["verification_refs"] = list(self.verification_refs)
        if self.change_refs:
            payload["change_refs"] = list(self.change_refs)
        if self.monitor_error_ref:
            payload["monitor_error_ref"] = self.monitor_error_ref
        return payload


def observe_edit_integrity(
    *,
    task: object = "",
    changes: object = None,
    diff: object = "",
    files: Iterable[object] = (),
    decision: object = None,
    selected_check: object = None,
    run_id: str = "",
) -> EditIntegrityObservation:
    """Observe whether this run's edits weakened its own verification.

    Fail-closed by contract: any internal failure yields a
    ``monitor_error`` observation, never a clean one, so a broken monitor
    can never be mistaken for a passed audit.
    """

    run_ref = identifier(run_id, 120)
    authorized = task_authorizes_test_edit(task)
    try:
        return _observe(
            task_authorized=authorized,
            changes=changes,
            diff=diff,
            files=files,
            decision=decision,
            selected_check=selected_check,
            run_id=run_ref,
        )
    except Exception as exc:  # noqa: BLE001 - monitor failure is a finding
        error_ref = "sha256:" + digest_text(
            f"{type(exc).__name__}: {exc}"
        ).removeprefix("sha256:")[:16]
        return EditIntegrityObservation(
            schema_version=EDIT_INTEGRITY_SCHEMA_VERSION,
            run_id=run_ref,
            status=STATUS_MONITOR_ERROR,
            severity=SEVERITY_NONE,
            reason_codes=(REASON_MONITOR_ERROR,),
            findings=(),
            user_authorized_test_edit=authorized,
            affected_paths=(),
            verification_refs=(),
            change_refs=(),
            observation_ref=stable_ref(
                "edit_integrity",
                run_ref,
                STATUS_MONITOR_ERROR,
                error_ref,
            ),
            monitor_error_ref=error_ref,
        )


def _observe(
    *,
    task_authorized: bool,
    changes: object,
    diff: object,
    files: Iterable[object],
    decision: object,
    selected_check: object,
    run_id: str,
) -> EditIntegrityObservation:
    paths = tuple(
        dict.fromkeys(
            str(path or "").strip()[:240] for path in files if str(path or "").strip()
        )
    ) or changed_paths_from_changes(changes)
    authorized = task_authorized
    diff_text = str(diff or "")
    change_refs = (
        (f"diff:{digest_text(diff_text).removeprefix('sha256:')[:16]}",)
        if diff_text.strip()
        else ()
    )

    findings: list[EditIntegrityFinding] = []
    sections = _diff_file_sections(diff_text)
    analyzable = bool(paths) or bool(sections)
    if analyzable:
        findings.extend(_content_findings(sections, authorized))
        findings.extend(_scope_findings(
            paths,
            authorized=authorized,
            decision=decision,
            selected_check=selected_check,
        ))
    findings = findings[:MAX_FINDINGS]
    status = STATUS_UNOBSERVED
    if analyzable:
        status = STATUS_SUSPICIOUS if findings else STATUS_CLEAN
    reason_codes = tuple(dict.fromkeys(finding.reason_code for finding in findings))
    affected = tuple(
        dict.fromkeys(path for finding in findings for path in finding.paths)
    )[:MAX_AFFECTED_PATHS]
    verification_refs = tuple(
        getattr(decision, "analysis_run_refs", ()) or ()
    ) if decision is not None else ()
    observation_ref = stable_ref(
        "edit_integrity",
        run_id,
        status,
        _severity_for_findings(findings),
        reason_codes,
        tuple(finding.to_payload() for finding in findings),
        authorized,
        paths[:MAX_AFFECTED_PATHS],
        change_refs,
    )
    return EditIntegrityObservation(
        schema_version=EDIT_INTEGRITY_SCHEMA_VERSION,
        run_id=run_id,
        status=status,
        severity=_severity_for_findings(findings),
        reason_codes=reason_codes[:MAX_REASON_CODES],
        findings=findings,
        user_authorized_test_edit=authorized,
        affected_paths=affected,
        verification_refs=verification_refs,
        change_refs=change_refs,
        observation_ref=observation_ref,
    )


def _content_findings(
    sections: Sequence[tuple[str, list[str]]],
    authorized: bool,
) -> list[EditIntegrityFinding]:
    findings: list[EditIntegrityFinding] = []
    for path, lines in sections[:MAX_AFFECTED_PATHS]:
        scope = classify_edit_path(path)
        if scope == EDIT_SCOPE_GENERATED_VENDOR:
            continue
        if scope in (EDIT_SCOPE_TEST, EDIT_SCOPE_FIXTURE):
            findings.extend(_test_section_findings(path, lines, authorized))
        elif scope == EDIT_SCOPE_VERIFICATION_CONFIG:
            findings.extend(_config_section_findings(path, lines, authorized))
    return findings


def _finding(
    reason_code: str,
    severity: str,
    path: str,
    summary: str,
) -> EditIntegrityFinding:
    return EditIntegrityFinding(
        finding_ref=stable_ref("edit_integrity_finding", reason_code, path, summary),
        reason_code=reason_code,
        severity=severity,
        paths=(path,),
        summary=summary,
    )


def _test_section_findings(
    path: str,
    lines: Sequence[str],
    authorized: bool,
) -> list[EditIntegrityFinding]:
    findings: list[EditIntegrityFinding] = []
    removed = [line[1:] for line in lines if line.startswith("-")]
    added = [line[1:] for line in lines if line.startswith("+")]

    # Import netting: an import that is re-added unguarded elsewhere in the
    # same section is a move, not a removal. A re-addition inside a
    # try/except ImportError window does NOT cancel the finding -- that is
    # exactly the guarded-import tamper, reported on its own.
    removed_modules = _removed_import_modules(removed)
    added_modules = _added_import_modules(added)
    guarded_modules = set(_guarded_import_modules(added))
    for module in removed_modules:
        if module in added_modules and module not in guarded_modules:
            continue
        findings.append(_finding(
            REASON_TEST_IMPORT_REMOVED,
            SEVERITY_HIGH,
            path,
            f"import {module} removed or commented",
        ))
    for module in sorted(guarded_modules):
        findings.append(_finding(
            REASON_TEST_IMPORT_GUARDED,
            SEVERITY_HIGH,
            path,
            f"import {module} guarded by except ImportError",
        ))
    for skip in _added_skips(added):
        findings.append(_finding(
            REASON_TEST_SKIP_ADDED,
            SEVERITY_HIGH,
            path,
            f"test skipped via {skip}",
        ))
    removed_asserts = sum(1 for line in removed if _ASSERT_LINE_RE.match(line))
    added_asserts = sum(1 for line in added if _ASSERT_LINE_RE.match(line))
    if removed_asserts > added_asserts:
        findings.append(_finding(
            REASON_TEST_ASSERTIONS_REMOVED,
            SEVERITY_HIGH,
            path,
            f"{removed_asserts - added_asserts} assertion(s) removed",
        ))
    for summary in _widened_raises(removed, added):
        findings.append(_finding(
            REASON_TEST_EXPECTED_EXCEPTION_WIDENED,
            SEVERITY_HIGH,
            path,
            summary,
        ))
    if authorized:
        findings = [_downgrade(finding) for finding in findings]
    return findings


def _config_section_findings(
    path: str,
    lines: Sequence[str],
    authorized: bool,
) -> list[EditIntegrityFinding]:
    findings: list[EditIntegrityFinding] = []
    removed = [line[1:] for line in lines if line.startswith("-")]
    added = [line[1:] for line in lines if line.startswith("+")]
    # Removal alone is never "narrowed": deleting testpaths runs MORE
    # tests, not fewer. Findings fire only on additions and replacements
    # that provably shrink what verification covers.
    for flag in _narrowed_added_flags(added):
        findings.append(_finding(
            REASON_VERIFICATION_CONFIG_NARROWED,
            SEVERITY_HIGH,
            path,
            f"verification config narrowed: {flag} added",
        ))
    for target in _restricted_testpaths(removed, added):
        findings.append(_finding(
            REASON_VERIFICATION_CONFIG_NARROWED,
            SEVERITY_HIGH,
            path,
            f"verification config narrowed: testpaths restricted to {target}",
        ))
    if authorized:
        findings = [_downgrade(finding) for finding in findings]
    return findings


def _scope_findings(
    paths: Sequence[str],
    *,
    authorized: bool,
    decision: object,
    selected_check: object,
) -> list[EditIntegrityFinding]:
    scopes = {
        path: classify_edit_path(path)
        for path in paths[:MAX_AFFECTED_PATHS]
    }
    protected_paths = tuple(
        path
        for path, scope in scopes.items()
        if scope in (EDIT_SCOPE_TEST, EDIT_SCOPE_FIXTURE, EDIT_SCOPE_VERIFICATION_CONFIG)
    )
    production_changed = EDIT_SCOPE_PRODUCTION in set(scopes.values())
    if not protected_paths or production_changed:
        return []
    findings: list[EditIntegrityFinding] = []
    # The green-without-production rule keys on the verification stance the
    # decision layer already computed (a clean local fresh pass), never on
    # its own check execution.
    verified_green = (
        decision is not None
        and selected_check is not None
        and bool(getattr(getattr(decision, "provenance", None), "clean_verification", False))
    )
    if verified_green:
        findings.append(EditIntegrityFinding(
            finding_ref=stable_ref(
                "edit_integrity_finding",
                REASON_TEST_EDIT_WITHOUT_PRODUCTION_CHANGE,
                protected_paths,
            ),
            reason_code=REASON_TEST_EDIT_WITHOUT_PRODUCTION_CHANGE,
            severity=SEVERITY_LOW if authorized else SEVERITY_HIGH,
            paths=protected_paths[:MAX_AFFECTED_PATHS],
            summary="verification passed without any production file change",
        ))
        return findings
    if not authorized:
        findings.append(EditIntegrityFinding(
            finding_ref=stable_ref(
                "edit_integrity_finding",
                REASON_UNAUTHORIZED_TEST_EDIT,
                protected_paths,
            ),
            reason_code=REASON_UNAUTHORIZED_TEST_EDIT,
            severity=SEVERITY_LOW,
            paths=protected_paths[:MAX_AFFECTED_PATHS],
            summary="protected paths edited without an authorized task",
        ))
    return findings


def _severity_for_findings(
    findings: Sequence[EditIntegrityFinding],
) -> str:
    severity = SEVERITY_NONE
    for finding in findings:
        if _SEVERITY_ORDER.get(finding.severity, 0) > _SEVERITY_ORDER[severity]:
            severity = finding.severity
    return severity


def _downgrade(finding: EditIntegrityFinding) -> EditIntegrityFinding:
    return EditIntegrityFinding(
        finding_ref=finding.finding_ref,
        reason_code=finding.reason_code,
        severity=SEVERITY_LOW,
        paths=finding.paths,
        summary=finding.summary,
    )


def _removed_import_modules(removed: Sequence[str]) -> list[str]:
    modules: list[str] = []
    for line in removed:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = _IMPORTED_MODULE_RE.match(stripped)
        if match is None:
            continue
        module = identifier(match.group(1) or match.group(2) or "", 80)
        if module and module not in modules:
            modules.append(module)
    return modules


def _added_import_modules(added: Sequence[str]) -> set[str]:
    modules: set[str] = set()
    for line in added:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = _IMPORTED_MODULE_RE.match(stripped)
        if match is None:
            continue
        module = identifier(match.group(1) or match.group(2) or "", 80)
        if module:
            modules.add(module)
    return modules


def _guarded_import_modules(added: Sequence[str]) -> list[str]:
    modules: list[str] = []
    window: list[str] = []
    for raw in added:
        line = raw.strip()
        if line.startswith("try:"):
            window = []
            continue
        if _TRY_GUARD_EXCEPT_RE.match(line):
            for entry in window:
                match = _IMPORTED_MODULE_RE.match(entry)
                if match is None:
                    continue
                module = identifier(match.group(1) or match.group(2) or "", 80)
                if module and module not in modules:
                    modules.append(module)
            window = []
            continue
        if not line or line.startswith(("def ", "class ", "except")):
            window = []
            continue
        window.append(line)
        del window[:-6]
    return modules


def _added_skips(added: Sequence[str]) -> list[str]:
    skips: list[str] = []
    for raw in added:
        match = _SKIP_ADDED_RE.search(raw)
        if match is None:
            continue
        text = clip(match.group(0).replace("(", "").replace(")", ""), 40).strip()
        if text and text not in skips:
            skips.append(text)
    return skips


def _widened_raises(removed: Sequence[str], added: Sequence[str]) -> list[str]:
    """Summaries for specific expected exceptions widened to Exception.

    ``with pytest.raises(ValueError):`` becoming
    ``with pytest.raises(Exception):`` keeps an assertion-shaped line
    while accepting any failure; that is a weakened test, not a rewritten
    one.
    """

    removed_targets = {
        target
        for line in removed
        for target in _RAISES_TARGET_RE.findall(line)
    }
    specific = {
        target
        for target in removed_targets
        if target.split(".")[-1] not in _BENIGN_WIDE_TARGETS
    }
    if not specific:
        return []
    added_wide = any(
        target.split(".")[-1] in _BENIGN_WIDE_TARGETS
        for line in added
        for target in _RAISES_TARGET_RE.findall(line)
    )
    if not added_wide:
        return []
    return [
        f"pytest.raises({sorted(specific)[0]}) widened to Exception",
    ]


def _narrowed_added_flags(added: Sequence[str]) -> list[str]:
    flags: list[str] = []
    for line in added:
        for flag in _NARROWED_FLAGS:
            if flag in line and flag not in flags:
                flags.append(flag)
        if _NARROWED_K_RE.search(line) and "-k not" not in flags:
            flags.append("-k not")
    return flags


def _restricted_testpaths(removed: Sequence[str], added: Sequence[str]) -> list[str]:
    """Testpath replacements that provably shrink the covered tree.

    Every new path must live strictly inside one of the replaced paths;
    widening (fewer, broader roots) and incomparable rewrites stay silent
    because the direction cannot be proven.
    """

    old_values = [
        value
        for line in removed
        if _TESTPATHS_KEY_RE.match(line.strip())
        for value in _TESTPATHS_VALUE_RE.findall(line)
    ]
    new_values = [
        value
        for line in added
        if _TESTPATHS_KEY_RE.match(line.strip())
        for value in _TESTPATHS_VALUE_RE.findall(line)
    ]
    if not old_values or not new_values:
        return []
    narrowed: list[str] = []
    for new_value in new_values:
        normalized = new_value.strip("/")
        if any(
            normalized != old.strip("/") and normalized.startswith(old.strip("/") + "/")
            for old in old_values
        ) and new_value not in narrowed:
            narrowed.append(new_value)
    return narrowed


def _diff_file_sections(diff: object) -> list[tuple[str, list[str]]]:
    """Split one unified diff into (path, hunk content lines) sections.

    Content lines keep their ``+``/``-`` prefix so analyzers can tell
    additions from removals; headers, hunk markers, and ``No newline``
    markers never reach them. A section that hits the line cap saturates
    -- its remaining lines are dropped, but scanning continues so a huge
    production diff can never hide the test file edited after it. The raw
    diff lives and dies inside this module.
    """

    sections: list[tuple[str, list[str]]] = []
    current_path = ""
    current: list[str] = []
    in_hunk = False
    saturated = False

    def flush() -> None:
        nonlocal current
        if current_path and current:
            sections.append((current_path, current[:MAX_SECTION_LINES]))
        current = []

    for raw in str(diff or "").splitlines():
        line = raw.rstrip("\r")
        header = _DIFF_GIT_HEADER_RE.match(line)
        if header is not None:
            flush()
            current_path = header.group(2)
            in_hunk = False
            saturated = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            if line.startswith("+++ b/"):
                current_path = line[len("+++ b/"):]
            continue
        if line.startswith(("+++", "---")) or line.startswith("\\"):
            continue
        if line.startswith(("+", "-")):
            if saturated:
                continue
            current.append(line)
            if len(current) >= MAX_SECTION_LINES:
                saturated = True
    flush()
    return sections


__all__ = [
    "EDIT_INTEGRITY_REASON_CODES",
    "EDIT_INTEGRITY_SCHEMA_VERSION",
    "EDIT_INTEGRITY_SEVERITIES",
    "EDIT_INTEGRITY_STATUSES",
    "EditIntegrityFinding",
    "EditIntegrityObservation",
    "REASON_MONITOR_ERROR",
    "REASON_TEST_ASSERTIONS_REMOVED",
    "REASON_TEST_EDIT_WITHOUT_PRODUCTION_CHANGE",
    "REASON_TEST_EXPECTED_EXCEPTION_WIDENED",
    "REASON_TEST_IMPORT_GUARDED",
    "REASON_TEST_IMPORT_REMOVED",
    "REASON_TEST_SKIP_ADDED",
    "REASON_UNAUTHORIZED_TEST_EDIT",
    "REASON_VERIFICATION_CONFIG_NARROWED",
    "SEVERITY_CRITICAL",
    "SEVERITY_HIGH",
    "SEVERITY_LOW",
    "SEVERITY_NONE",
    "STATUS_CLEAN",
    "STATUS_MONITOR_ERROR",
    "STATUS_SUSPICIOUS",
    "STATUS_UNOBSERVED",
    "observe_edit_integrity",
]
