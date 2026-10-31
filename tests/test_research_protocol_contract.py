from __future__ import annotations

import json
from dataclasses import fields

from codey.research.tool_contract import (
    PROTOCOL_DIRECT_ANSWER,
    PROTOCOL_INVALID_ARGS,
    PROTOCOL_NATIVE_SEARCH_LEAK,
    PROTOCOL_NO_JSON,
    PROTOCOL_TOO_MANY_TOOLS,
    PROTOCOL_UNKNOWN_TOOL,
    ToolContract,
    validate_tool_args,
)
from tests.support.research_protocol import JsonToolCodec


def parse(payload: dict) -> object:
    return JsonToolCodec().parse(json.dumps(payload))


def test_source_search_requires_query() -> None:
    plan = parse({"tool": "source_search", "args": {"url": "https://example.com/a"}})

    assert not plan.calls
    assert plan.control is None
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "source_search missing required arg 'query'" in plan.protocol_error


def test_source_search_rejects_queries_alias() -> None:
    plan = parse({
        "tool": "source_search",
        "args": {"url": "https://example.com/a", "queries": ["bootstrap validation", "other"]},
    })

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "source_search missing required arg 'query'" in plan.protocol_error


def test_unknown_args_do_not_pass_through_contract() -> None:
    plan = parse({
        "tool": "open_url",
        "args": {
            "url": "https://example.com/a",
            "offset": 12,
            "invented": "ignore me",
        },
    })

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "unexpected arg 'invented'" in plan.protocol_error


def test_web_search_rejects_queries_alias() -> None:
    plan = parse({"tool": "web_search", "args": {"queries": ["alpha safety", "backup"]}})

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "web_search missing required arg 'query'" in plan.protocol_error


def test_protocol_rejects_name_field_as_hidden_tool_alias() -> None:
    plan = parse({"name": "web_search", "args": {"query": "alpha"}})

    assert not plan.calls
    assert plan.control is None
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert 'exactly top-level "tool" and "args"' in plan.protocol_error


def test_protocol_rejects_top_level_args_shape() -> None:
    search = parse({"tool": "web_search", "query": "alpha"})
    done = parse({"tool": "done", "answer": "report"})

    assert not search.calls
    assert search.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert 'exactly top-level "tool" and "args"' in search.protocol_error
    assert done.control is None
    assert done.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert 'exactly top-level "tool" and "args"' in done.protocol_error


def test_protocol_rejects_extra_top_level_fields() -> None:
    cases = (
        {"tool": "web_search", "args": {"query": "alpha"}, "name": "done"},
        {"tool": "web_search", "args": {"query": "alpha"}, "query": "SECRET"},
    )

    for payload in cases:
        plan = parse(payload)

        assert not plan.calls
        assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
        assert 'exactly top-level "tool" and "args"' in plan.protocol_error


def test_protocol_rejects_extra_json_object_before_valid_tool_call() -> None:
    reply = "\n".join([
        json.dumps({"foo": "bar"}),
        json.dumps({"tool": "web_search", "args": {"query": "alpha"}}),
    ])

    plan = JsonToolCodec().parse(reply)

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_TOO_MANY_TOOLS
    assert "too many JSON tool calls" in plan.protocol_error


def test_protocol_rejects_non_plain_json_object_wrappers() -> None:
    payload = json.dumps({"tool": "web_search", "args": {"query": "alpha"}})
    cases = (
        f"[{payload}]",
        f"```json\n{payload}\n```",
        f"Here is the call: {payload}",
    )

    for reply in cases:
        plan = JsonToolCodec().parse(reply)

        assert not plan.calls
        assert plan.control is None
        assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
        assert "exactly one JSON object and nothing else" in plan.protocol_error


def test_typographic_json_quotes_are_not_accepted_by_generic_protocol_codec() -> None:
    plan = JsonToolCodec().parse(
        "{“tool”:“web_search”,“args”:{“query”:“arXiv retrieval augmented generation evaluation”}}"
    )

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_NO_JSON
    assert "no JSON tool call found" in plan.protocol_error


def test_open_url_numeric_strings_are_coerced_and_missing_optionals_default() -> None:
    """Retain the historical node id; defaults/coercion belong to execution."""
    plan = parse({"tool": "open_url", "args": {"url": "https://example.com/a", "offset": "12"}})

    assert plan.protocol_error == ""
    assert plan.calls[0].args == {"url": "https://example.com/a", "offset": "12"}


def test_open_url_invalid_optional_number_is_invalid_args() -> None:
    plan = parse({"tool": "open_url", "args": {"url": "https://example.com/a", "offset": "abc"}})

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "open_url arg 'offset' must be integer" in plan.protocol_error


def test_unknown_tool_is_typed_error() -> None:
    plan = parse({"tool": "browse_web", "args": {"query": "alpha"}})

    assert not plan.calls
    assert plan.control is None
    assert plan.protocol_error_kind == PROTOCOL_UNKNOWN_TOOL
    assert "unknown tool: browse_web" in plan.protocol_error


def test_legacy_tool_aliases_are_unknown() -> None:
    for tool in (
        "search",
        "open",
        "fetch",
        "read_url",
        "search_source",
        "find_in_source",
        "recall",
        "memory_search",
        "save_note",
        "write_note",
        "answer",
        "finish",
    ):
        plan = parse({"tool": tool, "args": {"query": "alpha", "answer": "done"}})

        assert not plan.calls
        assert plan.control is None
        assert plan.protocol_error_kind == PROTOCOL_UNKNOWN_TOOL
        assert f"unknown tool: {tool}" in plan.protocol_error


def test_research_tool_contract_has_no_alias_layer() -> None:
    assert "aliases" not in {field.name for field in fields(ToolContract)}


def test_multiple_known_tool_calls_are_typed_error() -> None:
    reply = "\n".join([
        json.dumps({"tool": "knowledge_search", "args": {"query": "alpha"}}),
        json.dumps({"tool": "web_search", "args": {"query": "alpha"}}),
    ])

    plan = JsonToolCodec().parse(reply)

    assert not plan.calls
    assert plan.control is None
    assert plan.protocol_error_kind == PROTOCOL_TOO_MANY_TOOLS
    assert "too many JSON tool calls" in plan.protocol_error


def test_direct_report_without_json_is_typed_error() -> None:
    plan = JsonToolCodec().parse("## 结论\nAlpha requires notice.\n\n## 来源\n[1] Alpha - https://example.com")

    assert not plan.calls
    assert plan.control is None
    assert plan.protocol_error_kind == PROTOCOL_DIRECT_ANSWER


def test_direct_report_with_plain_chinese_conclusion_is_typed_error() -> None:
    plan = JsonToolCodec().parse("结论：Alpha requires notice.\n\n来源：agency manual")

    assert not plan.calls
    assert plan.control is None
    assert plan.protocol_error_kind == PROTOCOL_DIRECT_ANSWER


def test_native_search_leak_without_json_is_typed_error() -> None:
    plan = JsonToolCodec().parse("I searched the web and search results show Alpha has a 72-hour threshold.")

    assert not plan.calls
    assert plan.control is None
    assert plan.protocol_error_kind == PROTOCOL_NATIVE_SEARCH_LEAK


def test_knowledge_write_missing_body_is_invalid_args() -> None:
    plan = parse({"tool": "knowledge_write", "args": {"type": "fact", "title": "Alpha"}})

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "knowledge_write missing required arg 'body'" in plan.protocol_error


def test_knowledge_write_single_evidence_object_normalizes_to_list() -> None:
    """Retain the historical node id; the shared schema rejects implicit wrapping."""
    evidence = {
        "claim": "Alpha requires notice.",
        "source_url": "https://agency.gov/alpha-safety/manual",
        "excerpt": "Alpha requires notice.",
        "stance": "supports",
    }

    plan = parse({
        "tool": "knowledge_write",
        "args": {
            "type": "fact",
            "title": "Alpha threshold",
            "body": "Alpha requires notice.",
            "sources": ["https://agency.gov/alpha-safety/manual"],
            "evidence": evidence,
        },
    })

    assert not plan.calls
    assert "arg 'evidence' must be array" in plan.protocol_error


def test_knowledge_write_evidence_list_requires_objects() -> None:
    plan = parse({
        "tool": "knowledge_write",
        "args": {
            "type": "fact",
            "title": "Alpha threshold",
            "body": "Alpha requires notice.",
            "sources": ["https://agency.gov/alpha-safety/manual"],
            "evidence": ["bad"],
        },
    })

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "knowledge_write arg 'evidence[0]' must be object" in plan.protocol_error


def test_knowledge_write_relations_list_requires_objects() -> None:
    plan = parse({
        "tool": "knowledge_write",
        "args": {
            "type": "hypothesis",
            "title": "Alpha link",
            "body": "Alpha may affect beta.",
            "relations": ["war->helium"],
        },
    })

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "knowledge_write arg 'relations[0]' must be object" in plan.protocol_error


def test_knowledge_write_single_relation_object_normalizes_to_list() -> None:
    """Retain the historical node id; the shared schema rejects implicit wrapping."""
    relation = {"src": "war", "dst": "helium supply", "kind": "affects"}

    plan = parse({
        "tool": "knowledge_write",
        "args": {
            "type": "hypothesis",
            "title": "Alpha link",
            "body": "Alpha may affect beta.",
            "relations": relation,
        },
    })

    assert not plan.calls
    assert "arg 'relations' must be array" in plan.protocol_error


def test_knowledge_write_synthesis_is_reserved_for_done() -> None:
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.protocols.text_turn import decode_text_turn

    state = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.write"}), strict_research=True),
                        task_kind="research", task_text="Research alpha", research_questions=("What is alpha?",))
    state.record_open("https://example.com/a", source_id="s1")
    plan = normalize_turn(decode_text_turn(json.dumps({"tool": "knowledge_write", "args": {
        "type": "synthesis", "title": "Report", "body": "final report"}})), snapshot=build_turn_snapshot(state))
    assert not plan.calls and plan.control is None
    assert "knowledge_write" in plan.protocol_error and "not allowed" in plan.protocol_error


def test_done_requires_summary_and_rejects_aliases() -> None:
    summary = JsonToolCodec().parse(json.dumps({"tool": "done", "args": {"summary": "report"}}))
    text = JsonToolCodec().parse(json.dumps({"tool": "done", "args": {"text": "report 2"}}))

    assert summary.control is not None
    assert summary.control.kind == "done"
    assert text.control is None
    assert text.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "done missing required arg 'summary'" in text.protocol_error


def test_source_search_disabled_is_unknown_tool() -> None:
    plan = JsonToolCodec(include_source_search=False).parse(json.dumps({
        "tool": "source_search",
        "args": {"url": "https://example.com/a", "query": "alpha"},
    }))

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_UNKNOWN_TOOL


def test_validate_tool_args_rejects_bad_optional_types_instead_of_defaulting() -> None:
    result = validate_tool_args("open_url", {"url": "https://example.com/a", "limit": "lots"})

    assert not result.ok
    assert result.error_kind == PROTOCOL_INVALID_ARGS
    assert "open_url arg 'limit' must be integer" in result.error
