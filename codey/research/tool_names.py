"""Tool groups used by research planning and its model contract."""

INVESTIGATION_TOOLS = frozenset({"web_search", "open_url", "open_result", "reopen_source", "open_hit",
                                 "source_search", "knowledge_search", "knowledge_read"})
QUESTION_BOUND_TOOLS = INVESTIGATION_TOOLS | {"save_finding", "save_inference"}
