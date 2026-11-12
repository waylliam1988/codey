"""Page prerequisites and exact receipt delivery for single-page uploads."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from codey.operations.browser_completion import committed_results
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import _browser_body, require_tool_success
from codey.runtime.core.models import ToolResult
from codey.task.web_intent import source_url_key
from codey.types.json_value import JsonObject

_UPLOAD = re.compile(r"上传|\b(?:upload|browser_upload)\b", re.I)
_NEGATED = re.compile(r"(?:不要|禁止|无需|不用|不必|勿)\s*(?:再\s*)?$|\b(?:do not|don't|never|without)\s*$", re.I)
_REPORT_HASH = re.compile(
    r"(?:报告|汇报|告诉我|返回|给出|告知|列出|提供|输出|\b(?:report|return|provide|give|include|show|list)\b)"
    r"[^。！？!?;；\n]{0,240}\bsha[ -]?256\b", re.I | re.ASCII)
_PAGE_TEXT = re.compile(r"内容标记|页面(?:正文|文本|显示)|\b(?:content marker|page text)\b", re.I)
_ATTACHMENT = re.compile(r"\batt_[0-9a-f]{64}\b")
_NAVIGATE = require_tool_success("browser_navigate")
_READ = require_tool_success("browser_read")
_PAGE_RECEIPTS = frozenset({"open_url", "browser_navigate"})
_SELECTION_RECEIPTS = frozenset({"browser_upload", "browser_read"})
UPLOAD_RECEIPTS = _SELECTION_RECEIPTS | {"browser_navigate"}


@dataclass(frozen=True)
class UploadScope:
    url: str
    attachment: str
    navigate: bool
    report_hash: bool
    read_page: bool
    chinese: bool


def upload_scope(session: TaskSession) -> UploadScope | None:
    if (session.task_kind != "chat" or session.policy.strict_research or len(session.required_source_urls) != 1
            or not session.policy.allows("browser.effect")):
        return None
    task = re.sub(r"```[\s\S]*?```", "", session.task_text)
    actions = list(_UPLOAD.finditer(task))
    attachments = set(_ATTACHMENT.findall(task))
    if not actions or any(_NEGATED.search(task[:match.start()]) for match in actions) or len(attachments) > 1:
        return None
    checks = session.policy.required_checks
    return UploadScope(next(iter(session.required_source_urls)), next(iter(attachments), ""),
        _NAVIGATE in checks or re.search(r"\bbrowser_navigate\b", task) is not None,
        _REPORT_HASH.search(task) is not None, _READ in checks or _PAGE_TEXT.search(task) is not None,
        bool(re.search(r"[\u3400-\u9fff]", task)))


def matches_document(document: object, url: str) -> bool:
    return (isinstance(document, dict) and isinstance(loader := document.get("loader_id"), str) and bool(loader)
            and isinstance(observed := document.get("url"), str) and source_url_key(observed) == url)


def browser_opened(result: ToolResult, url: str) -> bool:
    acquisition = result.canonical.get("source_acquisition")
    attempts = acquisition.get("attempts") if isinstance(acquisition, dict) else None
    requested = result.call.args.get("url")
    if (not result.ok or not isinstance(requested, str) or source_url_key(requested) != url
            or not isinstance(attempts, list) or not attempts or not isinstance(last := attempts[-1], dict)):
        return False
    return (last.get("backend") == "browser" and last.get("status") == "ok" and last.get("observation") == "content"
            and isinstance(final := last.get("final_url"), str) and source_url_key(final) == url)


def upload_page_ready(scope: UploadScope, document: JsonObject | None, results: Mapping[str, ToolResult]) -> bool:
    if not matches_document(document, scope.url):
        return False
    for result in results.values():
        if not result.ok or result.call.name not in _PAGE_RECEIPTS or result.audit.get("browser_document") != document:
            continue
        if result.call.name == "open_url":
            if not scope.navigate and browser_opened(result, scope.url):
                return True
        else:
            url = result.call.args.get("url")
            value = result.canonical.get("value")
            if (isinstance(url, str) and source_url_key(url) == scope.url and isinstance(value, dict)
                    and type(status := value.get("http_status")) is int and 200 <= status < 400):
                return True
    return False


@dataclass(frozen=True)
class UploadEvidence:
    name: str
    sha256: str
    text: str

    def render(self, *, chinese: bool) -> str:
        lines = ([f"已选择附件：{self.name}", f"选择回执 SHA256：{self.sha256}"] if chinese else
                 [f"Attachment selected: {self.name}", f"Selection receipt SHA256: {self.sha256}"])
        if self.text:
            lines.extend(["", "选择后的页面内容：" if chinese else "Page content after selection:", self.text])
        return "\n".join(lines)


def upload_evidence(session: TaskSession, scope: UploadScope, document: JsonObject | None) -> UploadEvidence | str:
    if not matches_document(document, scope.url):
        return "Return to the target upload page and verify its state before completing."
    selection: tuple[str, str] | None = None
    body = ""
    for _, result in committed_results(session.executed, session._memory_results, _SELECTION_RECEIPTS):
        if result.audit.get("browser_document") != document:
            continue
        if result.call.name == "browser_upload":
            value = result.canonical.get("value")
            if (not result.truncated and isinstance(value, dict) and value.get("delivery") == "verified"
                    and (not scope.attachment or value.get("attachment_id") == scope.attachment)
                    and isinstance(name := value.get("name"), str) and name
                    and isinstance(digest := value.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", digest)):
                selection, body = (name, digest), ""
        elif result.call.name == "browser_read" and selection is not None:
            try:
                body = _browser_body(result) or ""
            except (KeyError, TypeError, ValueError):
                body = ""
    if selection is None:
        return "Select the requested attachment on the target page and obtain its successful upload receipt."
    if scope.read_page and not body:
        return "Read the complete target page after selecting the attachment. Reuse the existing upload receipt."
    return UploadEvidence(*selection, body if scope.read_page else "")
