"""Pure model guidance for strict Research completion; no orchestration or I/O."""

import json
from typing import TYPE_CHECKING

from codey.policies.task_policy import TaskPolicy
from codey.research.tool_contract import knowledge_read_id_guidance
from codey.reviews import report_sections

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession


def render_research_observations(session: "TaskSession") -> str:
    """Rebuild bounded evidence state from receipts, never narrative checkpoints."""
    recent = []
    findings = []
    for identity, result in session._memory_results.items():
        name = result.call.name
        if name not in {"web_search", "open_url", "open_result", "open_hit", "reopen_source",
                        "source_search", "knowledge_write", "knowledge_commit"}:
            continue
        recent.append({"result_ref": identity, "tool": name, "ok": result.ok,
                       **({"error": result.model_text[:400]} if not result.ok else {})})
        if result.ok:
            observation = result.canonical.get("research_observation")
            items = (observation.get("evidence") if isinstance(observation, dict)
                     else result.canonical.get("evidence_items"))
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        findings.append({key: str(item.get(key, ""))[:600]
                                         for key in ("claim", "source_url", "excerpt", "stance")})
    state = {"opened_sources": dict(list(session.source_ids.items())[-10:]),
             "saved_notes": session.notes_saved, "saved_evidence_count": len(session.evidence),
             "recent_receipts": recent[-4:], "saved_findings": findings[-6:]}
    guidance = (
        "Research observations (execution receipts override narrative summaries):\n"
        + json.dumps(state, ensure_ascii=False)
        + "\nA search hit is not an opened source. A rejected write saved nothing. "
          "Keep the claim no stronger than the excerpt, preserving model/population, comparator, "
          "endpoint and uncertainty. Registration is not reported efficacy; a trend is not a significant result. "
    )
    if session.policy.allows("knowledge.write"):
        guidance += (
            "Before drafting, save each useful finding from an opened passage; copy one short contiguous "
            "original sentence, without ellipses or reconstructed numbers. "
            "After a save succeeds, investigate the next missing evidence instead of saving it again. "
        )
    if session.policy.allows("web.read"):
        guidance += (
            "Locate missing evidence with source_search using distinctive terms for the actual finding, "
            "and read the returned passage. A broad single word may find the wrong passage; refine it "
            "if the requested fact is absent. Do not reuse an earlier quote for a different finding."
        )
    if "中文" in session.task_text:
        guidance += ("\n\n本轮只补充证据，不撰写最终报告。" if session.task_kind == "evidence_followup"
                     else "\n\n本轮要求中文研究报告。")
        guidance += (
            "逐项处理用户要求的比较与限制；"
            "每项要有直接证据，查不到则明确写出该项缺口，不能凭一条背景知识声称全部覆盖。"
        )
        if session.policy.allows("knowledge.write"):
            guidance += (
                "每次 knowledge_write 先归档一项发现：evidence[].claim 写完整中文结论，excerpt 保留原文。"
                "目录标题不是结果；引文必须直接支持主张的判断、对象、条件及数字。"
                "笔记 body 中的其他说法没有因此获得证据；先补齐缺失发现。"
            )
        if session.task_kind == "evidence_followup":
            guidance += "结束时用 done.summary 简述实际保存的发现和剩余缺口，最终报告交由父任务处理。"
        elif session.policy.strict_research:
            guidance += (
                "done 使用 report_sections 时，summary 只放带引用的结论，五个数组放其余内容，"
                "不要把整篇报告再放入 summary。尚未满足反馈时，执行下一项调查动作，勿重复旧稿。"
            )
    return guidance


def render_completion_guidance(policy: TaskPolicy) -> str:
    titles = [report_sections.section_title(key) for key in report_sections.REQUIRED_SECTIONS]
    rules = (
        "Research evidence rules: select the next useful tool action and wait for its result. "
        "Keep planning brief; do not repeatedly restate the task or draft the report before gathering evidence. "
        "Search results are leads, not evidence. "
    )
    if policy.allows("web.read"):
        rules += (
            "Use web_search, then open_result or open_url before citing a source. "
            "Search ranking is not authority. When primary or official sources are requested, "
            "refine the query with a relevant site: filter or follow original citations until the original is opened. "
        )
    if policy.allows("knowledge.write"):
        rules += ("Save short exact excerpts with knowledge_write: type=fact, sources=[opened URL], "
                  "evidence=[{claim:conclusion,source_url:the same URL,excerpt:exact original text,stance:supports}]. "
                  "Each excerpt must support the actual claim, not merely mention the same topic. "
                  "Write evidence[].claim as a complete scoped sentence in the requested report language; "
                  "keep excerpt in its original language. "
                  "Only evidence[].claim becomes a reusable backed claim; the note body is a summary. "
                  "For two findings, save two evidence items with their respective direct excerpts. "
                  "Read omitted passages with source_search or offset reads before quoting them. ")
    rules += (
        "Use only local tool results as evidence, including when a web model has built-in browsing. "
        f"Finish with done and these report sections: {', '.join(titles)}. "
        "Cite only sources opened and saved in this run."
    )
    checklist = "Strict research completion checklist: "
    if policy.allows("knowledge.write"):
        checklist += "Do not call done before knowledge_write has saved evidence from an opened source. "
    else:
        checklist += (
            "Saved evidence is still required; without permission to save missing evidence, "
            "report the blocked requirement rather than claim completion. "
        )
    if policy.allows("knowledge.read"):
        checklist += (
            "After a completion rejection, repair the specific reported defect. "
            "Use knowledge_read only if saved evidence needs inspection; formatting errors require no new reads. "
        )
    else:
        checklist += "After a completion rejection, repair the report using the available tool results. "
    if policy.allows("knowledge.write"):
        checklist += (
            "Do not repeat knowledge_write for the same evidence; add a new evidence excerpt "
            "only when the opened source supports a new claim. "
        )
        if policy.allows("knowledge.read"):
            checklist += knowledge_read_id_guidance(source_tool="knowledge_write")
    report = (
        "Preferred report arguments: done.summary is the conclusion with [1]-style citations; "
        "done.report_sections={evidence:[text],counter:[text],source_quality:[text],coverage:[text],sources:[text]}. "
        "Each array contains the paragraphs for that section. The application adds headings and actual line breaks, "
        "so these values need no manually encoded newline separators. "
        "Alternatively supply a complete Markdown report in summary alone. "
        "When using summary alone, report format is strict: use these literal Markdown headings, each on its own line: "
        f"{', '.join('## ' + title for title in titles)}. "
        "Do not replace headings with bold labels or inline prose. Put [1]-style citations "
        "in 结论 and 关键证据. In 反证与限制 cite [n] for observed limitations or contrary results. "
        "If no contrary evidence was found, describe the actual search scope and remaining limits; "
        "if it was not investigated, say so. Do not copy a generic no-counter statement as evidence of a check. "
        "In 来源, list each cited source as "
        "[1] Title - https://...; use only URLs opened and saved in this run."
        " Preserve deleted/struck-through text as historical, not current guidance; check adjacent version exceptions. "
        "When using summary alone, it must contain actual multiline Markdown after argument decoding."
    )
    return "\n\n".join((rules, checklist.rstrip(), report))
