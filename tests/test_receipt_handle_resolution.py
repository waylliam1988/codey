from __future__ import annotations

import pytest


def _session(tmp_path):
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    return TaskSession(
        TaskPolicy(frozenset({"control", "project.read", "web.read"})),
        project=str(tmp_path),
    )


def _receipt(session, ref: str, *, name: str = "read_file", path: str = "a.txt") -> None:
    from codey.runtime.core.models import ToolCall, ToolResult

    call = ToolCall(name, {"path": path}, call_id=ref + ":call")
    session._memory_results[ref] = ToolResult(
        call,
        "text",
        ok=True,
        canonical={"value": {"path": path, "text": "text"}},
        audit={"result_ref": ref},
    )
    session.executed[ref] = {
        "name": name,
        "ok": True,
        "call_id": call.call_id,
        "excerpt": "text",
        "args_digest": "",
    }


def test_receipt_handles_are_derived_from_existing_results_and_stable(tmp_path) -> None:
    from codey.operations.reference_handles import ReferenceHandleResolver

    session = _session(tmp_path)
    _receipt(session, "task_turn_effect:run:1:0")
    resolver = ReferenceHandleResolver(session)

    first = resolver.resolve("rc1")
    assert first.kind == "receipt"
    assert first.ref == "task_turn_effect:run:1:0"
    assert first.tool_name == "read_file"

    _receipt(session, "task_turn_effect:run:1:1", path="b.txt")
    assert resolver.resolve("rc1").ref == first.ref
    assert resolver.resolve("rc2").ref == "task_turn_effect:run:1:1"


def test_source_and_evidence_handles_bind_to_existing_ledger_versions(tmp_path) -> None:
    from codey.operations.reference_handles import ReferenceHandleError, ReferenceHandleResolver
    from codey.research.ledger import EvidenceItem, ResearchLedger

    session = _session(tmp_path)
    url = "https://example.test/article"
    session.source_ids["s1"] = url
    ledger = ResearchLedger()
    ledger.record_open(url, url, "Article", "an exact passage")
    ledger.evidence_items.append(EvidenceItem(
        claim="claim", source_url=url, excerpt="an exact passage", locator="offset:0",
    ))
    resolver = ReferenceHandleResolver(session, ledger=ledger)

    source_handle = resolver.handle_for("s1")
    evidence_handle = resolver.handle_for("e1")
    source = resolver.resolve(source_handle)
    evidence = resolver.resolve(evidence_handle)
    assert source.kind == "source" and source.version
    assert evidence.kind == "evidence"
    assert evidence.source_ref == "s1"
    assert evidence.version == source.version

    ledger.record_open(url, url, "Article", "a new passage")
    with pytest.raises(ReferenceHandleError, match="different source version"):
        resolver.resolve(source_handle)
    with pytest.raises(ReferenceHandleError, match="different source version"):
        resolver.resolve(evidence_handle)


def test_namespaces_do_not_allow_receipt_or_source_alias_confusion(tmp_path) -> None:
    from codey.operations.reference_handles import ReferenceHandleError, ReferenceHandleResolver

    session = _session(tmp_path)
    _receipt(session, "task_turn_effect:run:1:0")
    session.search_results["r1"] = "https://example.test/search-result"
    resolver = ReferenceHandleResolver(session)

    assert resolver.resolve("rc1").kind == "receipt"
    assert resolver.resolve("r1").kind == "search_result"
    with pytest.raises(ReferenceHandleError):
        resolver.resolve("r2")


def test_read_tool_result_accepts_a_receipt_handle_without_copying_the_full_id(tmp_path) -> None:
    from codey.operations.tool_result_reader import read_tool_result
    from codey.runtime.core.models import ToolCall

    session = _session(tmp_path)
    _receipt(session, "task_turn_effect:run:1:0")

    class Context:
        managed_outputs = None
        session_id = "session"
        run_id = "run"

        def __init__(self, current):
            self.session = current

    result = read_tool_result(Context(session), ToolCall("read_tool_result", {"result_ref": "rc1"}))
    assert result.ok
    assert result.canonical["receipt_page"]["result_ref"] == "task_turn_effect:run:1:0"


def test_result_prompt_exposes_the_short_receipt_handle(tmp_path) -> None:
    from codey.operations.kernel_prompt import _result_context

    session = _session(tmp_path)
    _receipt(session, "task_turn_effect:run:1:0")
    result = session._memory_results["task_turn_effect:run:1:0"]
    prompt = _result_context(result, session)
    assert "Receipt display handle: rc1" in prompt


def test_receipt_handles_keep_nested_child_before_parent_after_projection_restore(tmp_path) -> None:
    from codey.operations.reference_handles import ReferenceHandleResolver
    from codey.runtime.core.models import ToolCall, ToolResult

    session = _session(tmp_path)
    child_ref = "task_turn_effect:child"
    parent_ref = "task_turn_effect:parent"
    child_call = ToolCall("read_file", {"path": "a.txt"}, call_id="parent:child:0")
    parent_call = ToolCall("codemode", {"code": "return 1;"}, call_id="parent")
    child = ToolResult(child_call, "child", ok=True, canonical={"value": "child"})
    parent = ToolResult(parent_call, "parent", ok=True, canonical={"value": 1, "child_effect_ids": [child_ref]})
    # Durable recovery inserts the parent intent before the nested child even
    # though the live projection commits the child first.
    session._memory_results[parent_ref] = parent
    session._memory_results[child_ref] = child
    session.executed[parent_ref] = {"name": "codemode", "ok": True, "call_id": "parent",
                                    "excerpt": "parent", "args_digest": "", "child_effect_ids": [child_ref]}
    session.executed[child_ref] = {"name": "read_file", "ok": True, "call_id": "parent:child:0",
                                   "excerpt": "child", "args_digest": ""}
    resolver = ReferenceHandleResolver(session)
    assert resolver.resolve("rc1").ref == child_ref
    assert resolver.resolve("rc2").ref == parent_ref
    assert resolver.handle_for(child_ref) == "rc1"


def test_evidence_handle_reuses_answer_scope_passage_without_evidence_items(tmp_path) -> None:
    from codey.operations.reference_handles import ReferenceHandleResolver
    from codey.research.ledger import ResearchLedger
    from codey.research.ledger_receipts import ledger_observation
    from codey.research.passages import describe_window, restore_passages
    from codey.runtime.core.models import ToolCall, ToolResult

    session = _session(tmp_path)
    url = "https://example.test/article"
    session.source_ids["s1"] = url
    ledger = ResearchLedger()
    text = "An exact production passage with enough words for a citation."
    ledger.record_open(url, url, "Article", text)
    source = ledger.opened_sources[-1]
    assert source is not None
    rows = describe_window(url, source.text_hash, text, 0, len(text))
    restore_passages(ledger, ledger_observation(ledger, "open", url=url), rows)
    session._memory_results["read"] = ToolResult(
        ToolCall("open_url", {"url": url}), text, ok=True, canonical={"read_passages": rows},
    )
    resolver = ReferenceHandleResolver(session, ledger=ledger)

    resolved = resolver.resolve("e1")
    assert resolved.kind == "evidence"
    assert resolved.source_ref == "s1"
    assert resolved.evidence is not None
    assert resolved.evidence.excerpt == text
    assert not ledger.evidence_items
