"""Render explicitly requested tab metadata from the current browser document."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codey.operations.browser_completion import committed_results
from codey.policies.output_requirement import is_output_check
from codey.policies.requirement import RequirementEvaluation
from codey.policies.tool_requirement import ToolObservation, ToolSuccessRequirement
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject, decode_json_object

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.execution_record import ExecutionRecord
    from codey.runtime.core.models import ToolResult

METADATA_TOOLS = frozenset({"browser_eval", "browser_tab_list"})
_PROPERTY = r"window\.[A-Za-z_$][A-Za-z0-9_$]*"
_READ_PROPERTY = re.compile(rf"\bbrowser_eval\b[^。；;\n]{{0,100}}?\b({_PROPERTY})(?![\w$.])", re.ASCII)
_PROPERTY_EXPRESSION = re.compile(rf"\s*(?:return[ \t]+)?({_PROPERTY})\s*;?\s*")
_REPORT = re.compile(r"(?:报告|返回|列出|给出|输出|告诉我|\b(?:report|return|list|provide|include|show)\b)([^。！？!?;；\n]{0,180})", re.I)
_NO_REPORT = re.compile(r"(?:不要|无需|不用|不必|禁止|勿|不|\bdo not|\bdon't|\bnever)\s*$", re.I)
_TOKEN = re.compile(r"\btoken\b|令牌", re.I | re.ASCII)
_URL = re.compile(r"\burl\b|网址|网页地址|标签页地址", re.I | re.ASCII)
_LABEL = (r"(?:(?:当前[ \t]*(?:绑定[ \t]*)?)?(?:标签页|网页|页面|页)[ \t]*(?:的[ \t]*)?(?:URL|地址|网址)"
          r"|(?:current[ \t]+)?(?:tab|page)[ \t]+URL|URL|(?:脚本[ \t]*|script[ \t]+)?(?:token|令牌))")
_FIELD_LABEL = rf"(?:[-*>][ \t]+)?(?:the[ \t]+)?(?:\*\*|__)?{_LABEL}(?:\*\*|__)?"
_FIELD_PREDICATE = r"(?:[:：=]|是|为|等于|\b(?:is|was|equals)\b|(?=https?://))"
_FIELD_END = r"(?:\\n|[。！？；，\n]|[.!?,;](?=[ \t]|$))"
_FIELD_VALUE = (r'(?:`[^`\n]+`|\[[^\]\n]+\]\(https?://[^\s)]+\)|<https?://[^\s>]+>|"[^"\n]+"|“[^”\n]+”'
                rf"|[^\s。！？；，]+?(?={_FIELD_END}|\s|$))")
_DRAFT_FIELD = re.compile(
    rf"(?im)(?:^|(?<=[。！？；，])|(?<=[.!?,;])(?=[ \t])|(?<=\\n)|(?:\\n)+)"
    rf"[ \t]*{_FIELD_LABEL}[ \t]*{_FIELD_PREDICATE}[ \t]*{_FIELD_VALUE}(?:[ \t]*{_FIELD_END})?"
)
_FENCED_CODE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")


@dataclass(frozen=True)
class BrowserTextReport:
    expression: str
    via: str
    chinese: bool


def report_scope(session: TaskSession) -> BrowserTextReport | None:
    policy = session.policy
    if (session.task_kind != "chat" or policy.strict_research or not policy.allows("browser.effect")
            or not policy.allows("browser.interact") or len(session.required_source_urls) > 1
            or any(is_output_check(check) for check in policy.required_checks)):
        return None
    task = _FENCED_CODE.sub("\n", session.task_text)
    properties = {match[1] for match in _READ_PROPERTY.finditer(task)}
    reports = [match[1] for match in _REPORT.finditer(task) if not _NO_REPORT.search(task[:match.start()])]
    if (len(properties) != 1 or not re.search(r"\bbrowser_tab_list\b", task, re.ASCII)
            or not any(_TOKEN.search(fields) and _URL.search(fields) for fields in reports)):
        return None
    return BrowserTextReport(next(iter(properties)), "codemode" if re.search(r"\bcodemode\b", task, re.I) else "",
                             bool(re.search(r"[\u3400-\u9fff]", task)))


@dataclass(frozen=True)
class BrowserMetadata:
    page_url: str
    token: str

    def render(self, prose: str, *, chinese: bool) -> str:
        prose = "".join(part if index % 2 else _DRAFT_FIELD.sub("", part)
                        for index, part in enumerate(_FENCED_CODE.split(prose))).strip()
        fields = (f"当前标签页 URL：{self.page_url}\n脚本 token：{self.token}" if chinese else
                  f"Current tab URL: {self.page_url}\nScript token: {self.token}")
        return f"{prose}\n\n{fields}" if prose else fields


@dataclass(frozen=True)
class MetadataPending:
    evaluation: RequirementEvaluation
    call: ToolCall | None = None


def _pending(observation: ToolObservation, call: ToolCall, feedback: str) -> MetadataPending:
    evaluation = observation.evaluation
    return MetadataPending(RequirementEvaluation(evaluation.reason_code or "observation_selector_unavailable",
        feedback + evaluation.feedback, evaluation.evidence_refs),
        None if evaluation.reason_code in {"tool_parent_missing", "observation_receipt_invalid", "conflicting_observations"}
        else call)


def observe_metadata(scope: BrowserTextReport, records: Mapping[str, ExecutionRecord],
                     results: Mapping[str, ToolResult], document: JsonObject | None) -> BrowserMetadata | MetadataPending:
    url = document.get("url") if document is not None else None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return MetadataPending(RequirementEvaluation("browser_document_missing",
            "Observe the current browser document before reporting its URL and script token."))
    eligible: set[str] = set()
    for ref, result in committed_results(records, results, METADATA_TOOLS):
        if result.truncated or result.audit.get("browser_document") != document:
            continue
        if result.call.name == "browser_eval":
            raw, code = result.canonical.get("value"), result.call.args.get("code")
            observed_value = decode_json_object(raw) if isinstance(raw, dict) else None
            match = _PROPERTY_EXPRESSION.fullmatch(code) if isinstance(code, str) else None
            expression: str = match[1] if match is not None else ""
            if (expression != scope.expression or observed_value is None or observed_value.get("truncated") is not False):
                continue
        eligible.add(ref)
    current = {ref: row for ref, row in records.items() if row["name"] not in METADATA_TOOLS or ref in eligible}
    tab = ToolSuccessRequirement("browser_tab_list", scope.via, answer_pointer="/page_url", result_pointer="/page_url")
    script = ToolSuccessRequirement("browser_eval", scope.via, answer_pointer="/token", result_pointer="/value")
    page = tab.observe(current, results=results, browser_document=document)
    token = script.observe(current, results=results, browser_document=document)
    if not page.evaluation.passed or page.value != url:
        return _pending(page, ToolCall("browser_tab_list", {}), "Use browser_tab_list on the current browser document. ")
    if (not token.evaluation.passed or not isinstance(value := token.value, str) or not value or value != value.strip()
            or value.splitlines() != [value]):
        return _pending(token, ToolCall("browser_eval", {"code": scope.expression}),
                        f"Read {scope.expression} with browser_eval on the current browser document. ")
    return BrowserMetadata(url, value)
