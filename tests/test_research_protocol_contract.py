from __future__ import annotations

import json
from dataclasses import fields

from codey.research.protocols import JsonToolCodec
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

    assert plan.protocol_error == ""
    assert "invented" not in plan.calls[0].args


def test_web_search_rejects_queries_alias() -> None:
    plan = parse({"tool": "web_search", "args": {"queries": ["alpha safety", "backup"]}})

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "web_search missing required arg 'query'" in plan.protocol_error


def test_protocol_rejects_name_field_as_hidden_tool_alias() -> None:
    plan = parse({"name": "web_search", "args": {"query": "alpha"}})

    assert not plan.calls
    assert plan.control is None
    assert plan.protocol_error_kind == PROTOCOL_UNKNOWN_TOOL
    assert "no known tool in reply" in plan.protocol_error


def test_protocol_rejects_top_level_args_shape() -> None:
    search = parse({"tool": "web_search", "query": "alpha"})
    done = parse({"tool": "done", "answer": "report"})

    assert not search.calls
    assert search.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "web_search args must be an object" in search.protocol_error
    assert done.control is None
    assert done.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "done args must be an object" in done.protocol_error


def test_typographic_json_quotes_are_not_accepted_by_generic_protocol_codec() -> None:
    plan = JsonToolCodec().parse(
        "{“tool”:“web_search”,“args”:{“query”:“arXiv retrieval augmented generation evaluation”}}"
    )

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_NO_JSON
    assert "no JSON tool call found" in plan.protocol_error


def test_open_url_numeric_strings_are_coerced_and_missing_optionals_default() -> None:
    plan = parse({"tool": "open_url", "args": {"url": "https://example.com/a", "offset": "12"}})

    assert plan.protocol_error == ""
    assert plan.calls[0].args["offset"] == 12
    assert plan.calls[0].args["limit"] == 6000
    assert plan.calls[0].args["pages"] == ""


def test_open_url_invalid_optional_number_is_invalid_args() -> None:
    plan = parse({"tool": "open_url", "args": {"url": "https://example.com/a", "offset": "abc"}})

    assert not plan.calls
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "open_url.offset must be an integer" in plan.protocol_error


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

    assert plan.protocol_error == ""
    assert plan.calls
    assert plan.calls[0].args["evidence"] == [evidence]


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
    assert "knowledge_write.evidence must be a list of objects" in plan.protocol_error


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
    assert "knowledge_write.relations must be a list of objects" in plan.protocol_error


def test_knowledge_write_single_relation_object_normalizes_to_list() -> None:
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

    assert plan.protocol_error == ""
    assert plan.calls
    assert plan.calls[0].args["relations"] == [relation]


def test_knowledge_write_synthesis_is_reserved_for_done() -> None:
    plan = parse({
        "tool": "knowledge_write",
        "args": {"type": "synthesis", "title": "Report", "body": "final report"},
    })

    assert not plan.calls
    assert plan.control is None
    assert plan.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "done required for final synthesis" in plan.protocol_error


def test_done_rejects_answer_arg_aliases() -> None:
    summary = JsonToolCodec().parse(json.dumps({"tool": "done", "args": {"summary": "report"}}))
    text = JsonToolCodec().parse(json.dumps({"tool": "done", "args": {"text": "report 2"}}))

    assert summary.control is None
    assert summary.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "done missing required arg 'answer'" in summary.protocol_error
    assert text.control is None
    assert text.protocol_error_kind == PROTOCOL_INVALID_ARGS
    assert "done missing required arg 'answer'" in text.protocol_error


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
    assert "open_url.limit must be an integer" in result.error
