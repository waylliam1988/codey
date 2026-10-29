"""Pure model guidance for strict Research completion; no orchestration or I/O."""

from codey.policies.task_policy import TaskPolicy
from codey.research.tool_contract import knowledge_read_id_guidance
from codey.reviews import report_sections


def render_completion_guidance(policy: TaskPolicy) -> str:
    titles = [report_sections.section_title(key) for key in report_sections.REQUIRED_SECTIONS]
    rules = "Research evidence rules: search results are leads, not evidence. "
    if policy.allows("web.read"):
        rules += "Use web_search, then open_result or open_url before citing a source. "
    if policy.allows("knowledge.write"):
        rules += ("Save short exact excerpts with knowledge_write: type=fact, sources=[opened URL], "
                  "evidence=[{claim:conclusion,source_url:the same URL,excerpt:exact original text,stance:supports}]. "
                  "Each excerpt must support the actual claim, not merely mention the same topic. "
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
        "in 结论 and 关键证据; 反证与限制 must cite [n] or explicitly say 未找到强反证 "
        "and state what was searched. A valid no-counter example is exactly: "
        "- 未找到强反证；本轮仅检索并打开上述来源。[1]. In 来源, list each cited source as "
        "[1] Title - https://...; use only URLs opened and saved in this run."
        " Preserve deleted/struck-through text as historical, not current guidance; check adjacent version exceptions. "
        "When using summary alone, it must contain actual multiline Markdown after argument decoding."
    )
    return "\n\n".join((rules, checklist.rstrip(), report))
