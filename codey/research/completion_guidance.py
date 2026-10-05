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
        rules += "Save short exact excerpts with knowledge_write. "
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
            "After a completion rejection, use knowledge_read to inspect the saved evidence "
            "and repair the report before trying done again. "
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
        "Report format is strict: use these literal Markdown headings, each on its own line: "
        f"{', '.join('## ' + title for title in titles)}. "
        "Do not replace headings with bold labels or inline prose. Put [1]-style citations "
        "in 结论 and 关键证据; 反证与限制 must cite [n] or explicitly say 未找到强反证 "
        "and state what was searched. A valid no-counter example is exactly: "
        "- 未找到强反证；本轮仅检索并打开上述来源。[1]. In 来源, list each cited source as "
        "[1] Title - https://...; use only URLs opened and saved in this run."
    )
    return "\n\n".join((rules, checklist.rstrip(), report))
