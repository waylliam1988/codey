"""Original user requirements for source-backed changes and browser verification."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from codey.automation.browser_document import same_document
from codey.automation.browser_receipts import (
    PAGE_EVIDENCE_INVALIDATORS,
    committed_results,
    delivery,
    page_effect_possible,
)
from codey.completion.contract import CompletionCheck
from codey.operations.browser.browser_references import BrowserReferences
from codey.operations.kernel_provenance import _kernel_workspace_identity_of
from codey.policies.network import check_fetch_url
from codey.research.browser_result_evidence import complete_page_passages
from codey.research.source_observation import source_observation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.instruction_text import instruction_text, quoted_value
from codey.task.web_intent import LIVE_RECORD_ACTION, explicit_web_intent, source_url_key
from codey.types.json_value import JsonObject, decode_json_value, parse_json_object
from codey.workspace.paths import path_hash, safe_join

if TYPE_CHECKING:
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.execution_record import ExecutionRecord

class ProjectDeliverySession(Protocol):
    @property
    def project(self) -> str: ...
    @property
    def task_text(self) -> str: ...
    @property
    def policy(self) -> TaskPolicy: ...
    @property
    def executed(self) -> Mapping[str, ExecutionRecord]: ...
    @property
    def _memory_results(self) -> Mapping[str, ToolResult]: ...
    @property
    def source_ids(self) -> dict[str, str]: ...
    @property
    def handoff_source_urls(self) -> tuple[str, ...]: ...
    @property
    def workspace_revision(self) -> int: ...
    @property
    def workspace_fingerprint(self) -> str: ...
    def source_id_for(self, url: str) -> str: ...


SOURCE_BEFORE_EDIT = "project_live_record_before_edit"
PAGE_AFTER_EDIT = "project_live_page_after_edit"
_LIVE_SOURCE = re.compile(LIVE_RECORD_ACTION + r"\s+<?(https?://[^\s<>]+)", re.I)
_LIVE_SUBJECT = re.compile(r"\brecord\s+for\s+([^.!?\n]{1,64}?)\s+to\s+" + _LIVE_SOURCE.pattern, re.I)
_LIVE_PAGE = re.compile(
    r"\b(?:verify|validate|check)\s+(?:the\s+)?updated\s+[^.!?\n]{1,70}?\s+"
    r"(?:in|on)\s+(?:(?:its|the|a)\s+)?(?:browser(?:\s+page)?|live\s+page)\b"
    r"|(?:在|到)[^。！？\n]{0,24}(?:页面|网页|浏览器)[^。！？\n]{0,24}(?:核验|验证)[^。！？\n]{0,24}(?:修改|更新|结果)",
    re.I,
)
_VERIFY_PAGE_URL = re.compile(r"\s+(?:at\s+)?<?(https?://[^\s<>]+)", re.I)
_EDIT_SCOPE = re.compile(
    r"(?:^|[.!?;。；\n]\s+)(?:please\s+)?(?:"
    r"(?:only\s+(?:edit|modify|change)|(?:edit|modify|change)\s+only)"
    r"|create\s+exactly\s+one\s+file\s+at(?:\s+the)?(?:\s+project-relative\s+path)?"
    r"|(?:只|仅)(?:修改|编辑|改动))(?=\s|[\x60'\"“‘])", re.I,
)
_FILE_PATH = re.compile(r"(?P<path>(?:[^\s,;:<>?*\"'\x60/\\]+[/\\])*[^\s,;:<>?*\"'\x60/\\]+\.[A-Za-z0-9_-]{1,12})(?=$|[\s.!?;。；])")
_EDIT_TARGET = re.compile(r"(?:^|[.!?;。；\n]\s+)(?:please\s+)?(?P<verb>add|create|edit|modify|update)\b", re.I)
_PROJECT_LINK = re.compile(r"^(?:[-*]\s+)?(?P<role>record specification|application browser page):\s*"
                           r"<?(?P<url>https?://[^\s<>]+)>?\s*$", re.I | re.M)
_RECORD_INTERFACE = re.compile(r"^(?:[-*][ \t]+)?record interface:[ \t]+`([^`\r\n]+)`[ \t]*$", re.I | re.M)
_RECORD_JSON_FIELDS = re.compile(r"^(?:[-*][ \t]+)?record JSON fields in `([^`\r\n]+)`:[ \t]+"
                                 r"(`[^`\r\n]+`(?:,[ \t]*`[^`\r\n]+`){0,31})[ \t]*$", re.I | re.M)
_CITE_SOURCES = re.compile(r"(?:^|[.!?。！？;；\n]\s*)(?:(?:please\s+)?cite\b|(?:请)?引用)", re.I)


@dataclass(frozen=True)
class ProjectDeliveryLinks:
    specification: str = ""
    application: str = ""


@dataclass(frozen=True)
class ProjectFileBasis:
    path: str
    read_ref: str = ""
    version: str = ""


def _delivery_documentation(session: ProjectDeliverySession) -> tuple[str, ...]:
    root = Path(session.project)
    return tuple(text for ref, result in session._memory_results.items()
                 if ref in session.executed and result.call.name == "read_file" and result.ok
                 and (text := _current_documentation_text(root, result)))


def observed_delivery_links(session: ProjectDeliverySession) -> ProjectDeliveryLinks:
    checks = session.policy.required_checks
    task = instruction_text(session.task_text)
    wants_specification = SOURCE_BEFORE_EDIT in checks and "documented record format" in task.casefold()
    wants_application = PAGE_AFTER_EDIT in checks
    if not session.project or not (wants_specification or wants_application):
        return ProjectDeliveryLinks()
    groups: dict[str, set[str]] = {}
    for text in _delivery_documentation(session):
        for match in _PROJECT_LINK.finditer(text):
            if check_fetch_url(match["url"], resolve=False) is None:
                groups.setdefault(match["role"].casefold(), set()).add(match["url"])
    spec, app = (groups.get(role, set()) for role in ("record specification", "application browser page"))
    declared = {url for clause in _LIVE_PAGE.finditer(task)
                if (page_match := _VERIFY_PAGE_URL.match(task, clause.end())) is not None
                and check_fetch_url(url := page_match[1].rstrip(".,;:!?。；"), resolve=False) is None}
    application = declared or app
    return ProjectDeliveryLinks(next(iter(spec)) if wants_specification and len(spec) == 1 else "",
                                next(iter(application)) if wants_application and len(application) == 1 else "")


def documented_verification_paths(session: ProjectDeliverySession) -> tuple[str, ...]:
    if not session.project or not {SOURCE_BEFORE_EDIT, PAGE_AFTER_EDIT} <= set(session.policy.required_checks):
        return ()
    root = Path(session.project).resolve()
    paths: dict[str, None] = {}
    for text in _delivery_documentation(session):
        for path in _RECORD_INTERFACE.findall(text):
            try:
                target = safe_join(root, path)
                if target.is_file():
                    paths[target.relative_to(root).as_posix()] = None
            except (OSError, ValueError):
                continue
    return tuple(paths)


def documented_record_fields(session: ProjectDeliverySession | None, path: str) -> tuple[str, ...]:
    if session is None or not session.project or SOURCE_BEFORE_EDIT not in session.policy.required_checks:
        return ()
    schemas = {tuple(sorted(re.findall(r"`([^`]+)`", fields))) for text in _delivery_documentation(session)
               for target, fields in _RECORD_JSON_FIELDS.findall(text) if target == path}
    if len(schemas) > 1:
        raise ValueError(f"The observed documentation has conflicting JSON field lists for {path}.")
    return next(iter(schemas), ())


def record_field_error(path: str, fields: tuple[str, ...], content: str) -> str:
    try:
        if set(parse_json_object(content)) == set(fields):
            return ""
    except ValueError:
        pass
    return f"The documented JSON object for {path} has exactly these fields: {', '.join(fields)}."


def _current_documentation_text(root: Path, result: ToolResult) -> str:
    path, value, version = result.call.args.get("path"), result.canonical.get("value"), result.canonical.get("file_version")
    if (not isinstance(path, str) or not isinstance(value, dict) or not isinstance(version, str)
            or Path(path).suffix.casefold() not in {".md", ".rst", ".txt"}
            or value.get("start_line") != 1 or value.get("next_offset") != 0
            or value.get("text_complete") is not True or not isinstance(text := value.get("text"), str)
            or text not in result.model_text):
        return ""
    try:
        return text if "raw:" + path_hash(safe_join(root, path), preserve_newlines=True) == version else ""
    except (OSError, ValueError):
        return ""


def declared_edit_scope(project: Path, task: str) -> tuple[str, ...]:
    match = _EDIT_SCOPE.search(instruction_text(task))
    if match is None:
        return ()
    target, suffix = _leading_edit_path(task[match.end():].lstrip())
    if not target or suffix.strip() and suffix.lstrip()[0] not in ".!?;。；":
        return ()
    root = project.resolve()
    return (safe_join(root, target).relative_to(root).as_posix(),)


def declared_edit_target(task: str) -> str:
    return _named_edit_target(task)[0]


def _named_edit_target(task: str) -> tuple[str, str]:
    match = _EDIT_TARGET.search(instruction_text(task))
    return (_leading_edit_path(task[match.end():].lstrip())[0], match["verb"].casefold()) if match else ("", "")


def _leading_edit_path(target_clause: str) -> tuple[str, str]:
    target = quoted_value(target_clause)
    if target is not None:
        width = len(target_clause) - len(target_clause.lstrip("`")) if target_clause.startswith("`") else 1
        suffix = target_clause[len(target) + 2 * width:]
    else:
        file_match = _FILE_PATH.match(target_clause)
        if file_match is None:
            return "", ""
        target, suffix = file_match["path"], target_clause[file_match.end():]
    return (target, suffix) if target and _FILE_PATH.fullmatch(target) else ("", "")


def edit_scope_violation(project: Path, path: str, scope: tuple[str, ...], *, task: str = "") -> str:
    root = project.resolve()
    if scope:
        if safe_join(root, path) not in {safe_join(root, allowed) for allowed in scope}:
            return "The user authorized editing only " + ", ".join(scope) + ". No other file may be changed."
        return ""
    intended, verb = _named_edit_target(task)
    if not intended or verb not in {"add", "create"}:
        return ""
    try:
        requested = safe_join(root, intended)
    except (OSError, ValueError):
        return ""
    target = safe_join(root, path)
    alternate = target.relative_to(root).as_posix()
    if (target != requested and target.name.casefold() == requested.name.casefold()
            and re.search(r"(?<![\w./-])" + re.escape(alternate) + r"(?![\w/-])", task.replace("\\", "/"), re.I) is None):
        return f"The user requested adding {requested.relative_to(root).as_posix()}. Use that path for the named file."
    return ""


def declared_project_checks(task: str) -> tuple[str, ...]:
    text = instruction_text(task)
    source = _LIVE_SOURCE.search(text)
    source_required = source is not None and source_url_key(source[1].rstrip(".,;:!?")) in explicit_web_intent(task).urls
    return (*((SOURCE_BEFORE_EDIT,) if source_required else ()),
            *((PAGE_AFTER_EDIT,) if _LIVE_PAGE.search(text) else ()))


def _read_url(result: ToolResult) -> str:
    if (not result.ok or result.truncated or result.call.name not in {"open_url", "open_result", "browser_read"}
            or not result.canonical.get("read_passages")):
        return ""
    try:
        source, _, _ = source_observation(result.canonical.get("research_observation"))
    except (TypeError, ValueError):
        return ""
    return source.final_url


def _read_ref_for_url(session: ProjectDeliverySession, url: str) -> str:
    return next((ref for ref, result in reversed(tuple(session._memory_results.items()))
                 if ref in session.executed and _read_url(result) == url), "")


def live_source_basis(session: ProjectDeliverySession) -> tuple[str, ToolResult] | None:
    match = _LIVE_SOURCE.search(instruction_text(session.task_text))
    if match is None:
        return None
    target = source_url_key(match[1].rstrip(".,;:!?"))
    for ref, result in reversed(tuple(session._memory_results.items())):
        if ref not in session.executed:
            continue
        if (result.call.name in PAGE_EVIDENCE_INVALIDATORS and page_effect_possible(result)
                and _action_page(result) == target):
            return None
        url = _read_url(result)
        if url and source_url_key(url) == target:
            return (ref, result) if _subject_selection_verified(session, result) else None
    return None


def _live_subject(task: str) -> str:
    match = _LIVE_SUBJECT.search(instruction_text(task))
    return " ".join(match[1].split()) if match is not None else ""


def _subject_selection_verified(session: ProjectDeliverySession, read: ToolResult) -> bool:
    subject = _live_subject(session.task_text)
    document = read.audit.get("browser_document")
    if not subject:
        return True
    if not isinstance(document, dict):
        return False
    latest_fill: ToolResult | None = None
    for ref, result in session._memory_results.items():
        if result is read:
            break
        if (ref in session.executed and result.call.name == "browser_fill" and result.ok and delivery(result) == "verified"
                and same_document(document, result.audit.get("browser_document"))):
            latest_fill = result
    if latest_fill is not None:
        return latest_fill.call.args.get("value") == subject
    return _observed_form_state(session, document) is False


def _observed_form_state(session: ProjectDeliverySession, document: object) -> bool | None:
    for ref, result in reversed(tuple(session._memory_results.items())):
        if (ref not in session.executed or not result.ok or result.call.name != "browser_snapshot"
                or not same_document(document, result.audit.get("browser_document"))):
            continue
        raw = result.canonical.get("value")
        elements: object = raw.get("elements") if isinstance(raw, dict) else None
        if isinstance(raw, dict) and raw.get("elements_truncated") is False and isinstance(elements, list):
            return any(isinstance(row, dict) and row.get("disabled") is False
                       and row.get("role") in {"textbox", "searchbox", "combobox"} for row in elements)
    return None


def live_record_subject_call(session: ProjectDeliverySession, references: BrowserReferences | None,
                             document: JsonObject | None) -> ToolCall | None:
    if SOURCE_BEFORE_EDIT not in session.policy.required_checks or references is None or document is None:
        return None
    match = _LIVE_SUBJECT.search(instruction_text(session.task_text))
    url = document.get("url")
    if match is None or not isinstance(url, str) or source_url_key(url) != source_url_key(match[2].rstrip(".,;:!?")):
        return None
    subject = " ".join(match[1].split())
    fills = [result for ref, result in session._memory_results.items() if ref in session.executed and result.ok
             and result.call.name == "browser_fill" and delivery(result) == "verified"
             and same_document(result.audit.get("browser_document"), document)]
    if fills and fills[-1].call.args.get("value") == subject:
        return None
    choices = references.choices("browser_fill")
    return ToolCall("browser_fill", {"ref": choices[0], "value": subject}) if len(choices) == 1 else None


def project_file_basis(session: ProjectDeliverySession, project: Path, path: str, args: JsonObject) -> ProjectFileBasis:
    root = project.resolve()
    target = safe_join(root, path)
    relative = target.relative_to(root).as_posix()
    if not target.exists():
        return ProjectFileBasis(relative)
    explicit = args.get("base_result_ref")
    for ref, read in reversed(tuple(session._memory_results.items())):
        if (ref not in session.executed or not read.ok or read.call.name != "read_file"
                or isinstance(explicit, str) and explicit != ref):
            continue
        read_path, version = read.call.args.get("path"), read.canonical.get("file_version")
        if (isinstance(read_path, str) and isinstance(version, str) and version.startswith("raw:sha256:")
                and safe_join(root, read_path) == target):
            if version != "raw:" + path_hash(target, preserve_newlines=True):
                raise ValueError("The file changed since the selected read. Read the file again before editing.")
            return ProjectFileBasis(relative, ref, version)
    raise ValueError("Read the target file in this task before editing it from the live record.")


def project_source_binding(basis: tuple[str, ToolResult], file: ProjectFileBasis) -> JsonObject:
    ref, result = basis
    return {"source_result_ref": ref, "source_url": _read_url(result),
            "read_passages": decode_json_value(result.canonical["read_passages"]),
            "path": file.path, "base_result_ref": file.read_ref, "file_version": file.version}


def project_write_basis(session: ProjectDeliverySession) -> tuple[tuple[str, ToolResult] | None, str]:
    if SOURCE_BEFORE_EDIT not in session.policy.required_checks:
        return None, ""
    basis = live_source_basis(session)
    if basis is None:
        return None, "Read the observed live record before editing the project."
    specification = observed_delivery_links(session).specification
    if specification and not _read_ref_for_url(session, specification):
        return None, "Read the record specification listed in the project documentation before editing."
    return basis, ""


def project_citation_labels(session: ProjectDeliverySession) -> tuple[str, ...]:
    """Select the actual record and documented format used for the requested edit."""
    if SOURCE_BEFORE_EDIT not in session.policy.required_checks or not user_requests_citations(session.task_text):
        return ()
    refs = dict.fromkeys(ref for _, edit in committed_results(session.executed, session._memory_results, frozenset({"edit"}))
                         if isinstance(basis := edit.canonical.get("project_source_basis"), dict)
                         and isinstance(ref := basis.get("source_result_ref"), str))
    if not refs:
        return ()
    if (spec := observed_delivery_links(session).specification) and (ref := _read_ref_for_url(session, spec)):
        refs[ref] = None
    labels: dict[str, None] = {}
    for ref in refs:
        read = session._memory_results.get(ref)
        if (ref in session.executed and read is not None and (url := _read_url(read))
                and isinstance(label := read.canonical.get("source_id"), str)
                and session.source_ids.get(label) == url):
            labels[label] = None
    return tuple(labels)


def original_handoff_citation(session: ProjectDeliverySession) -> tuple[str, str] | None:
    if len(session.handoff_source_urls) != 1 or not user_requests_citations(session.task_text):
        return None
    url, = session.handoff_source_urls
    return session.source_id_for(url), url


def user_requests_citations(task: str) -> bool:
    return _CITE_SOURCES.search(instruction_text(task)) is not None


def _action_page(result: ToolResult) -> str:
    raw = result.canonical.get("value")
    if isinstance(raw, Mapping):
        page: object = raw.get("page_url")
        if isinstance(page, str):
            return source_url_key(page)
    return ""


def verified_project_page(session: ProjectDeliverySession, current_document: JsonObject | None, *, expected_page: str) -> str:
    if current_document is None:
        return ""
    candidate = ""
    wrote = False
    source = _LIVE_SOURCE.search(instruction_text(session.task_text))
    source_url = source_url_key(source[1].rstrip(".,;:!?")) if source is not None else ""
    for ref, result in session._memory_results.items():
        if ref not in session.executed:
            continue
        if result.call.name == "edit" and result.ok and result.audit.get("changed") is True:
            wrote, candidate = True, ""
        elif result.call.name in PAGE_EVIDENCE_INVALIDATORS and page_effect_possible(result):
            candidate = ""
        elif wrote and result.call.name == "browser_read" and (url := _read_url(result)):
            identity = _kernel_workspace_identity_of(result)
            if (identity is not None and identity.revision == session.workspace_revision
                    and identity.fingerprint == session.workspace_fingerprint
                    and (not expected_page or url == expected_page)
                    and (expected_page or source_url_key(url) != source_url)
                    and same_document(result.audit.get("browser_document"), current_document)
                    and complete_page_passages(result, url)):
                candidate = ref
    return candidate


def project_delivery_context(session: ProjectDeliverySession, document: JsonObject | None, links: ProjectDeliveryLinks) -> JsonObject:
    checks = session.policy.required_checks
    rows: JsonObject = {}
    if session.policy.edit_scope:
        rows["authorized_edit_paths"] = list(session.policy.edit_scope)
    if paths := documented_verification_paths(session):
        rows["documented_record_interfaces"] = list(paths)
    if SOURCE_BEFORE_EDIT in checks:
        basis = live_source_basis(session)
        subject = _live_subject(session.task_text)
        rows["live_record"] = {"result_ref": basis[0]} if basis else {
            "state": "select the requested record and read its result before editing",
            **({"requested_subject": subject} if subject else {}),
        }
    if PAGE_AFTER_EDIT in checks:
        page = verified_project_page(session, document, expected_page=links.application)
        rows["updated_application_page"] = {"result_ref": page} if page else {
            "state": "read the updated application page after the final edit"}
    if links.specification:
        spec_ref = _read_ref_for_url(session, links.specification)
        rows["record_specification"] = {"result_ref": spec_ref} if spec_ref else {"state": "unread"}
    return rows


def project_delivery_checks(session: ProjectDeliverySession, document: JsonObject | None) -> list[CompletionCheck]:
    checks: list[CompletionCheck] = []
    required = session.policy.required_checks
    if SOURCE_BEFORE_EDIT in required:
        bound = any(ref in session.executed and result.ok and result.call.name == "edit"
                    and bool(result.canonical.get("project_source_basis")) for ref, result in session._memory_results.items())
        checks.append(CompletionCheck(SOURCE_BEFORE_EDIT, "pass" if bound else "not_run",
                                      "" if bound else "original_record_read_before_edit_missing"))
    if PAGE_AFTER_EDIT in required:
        current = bool(verified_project_page(session, document, expected_page=observed_delivery_links(session).application))
        checks.append(CompletionCheck(PAGE_AFTER_EDIT, "pass" if current else "not_run",
                                      "" if current else "read_updated_page_after_last_edit"))
    return checks
