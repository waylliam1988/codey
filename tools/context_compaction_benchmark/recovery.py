"""Held-out continuation with genuine task-owned output reads on every arm."""
import hashlib
import json
from types import SimpleNamespace


def replay_response_schema(text):
    """Constrain syntax identically, never disclose expected values to a model."""
    if not isinstance(text, str) or text.startswith('<source>'):
        return None
    if text.startswith('Continue the review.'):
        properties = {'next_action': {'type': 'string', 'maxLength': 160}}
    elif text.startswith('For module_') and 'First read the selected module receipt' in text:
        properties = {'read': {'type': 'object', 'properties': {
            'result_ref': {'type': 'string'}, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}},
            'required': ['result_ref', 'offset', 'limit'], 'additionalProperties': False}}
    elif text.startswith(('Return exactly one JSON object', 'For module_', 'Read-only stored output')):
        properties = {key: {'type': ['string', 'null']} for key in ('constraint', 'target', 'result_ref')}
        properties['exit_code'] = {'type': ['integer', 'null']}
        if not text.startswith('Return exactly one JSON object'):
            properties['observations'] = {'type': 'object', 'properties': {
                key: {'type': ['string', 'null']} for key in ('middle', 'tail')},
                'required': ['middle', 'tail'], 'additionalProperties': False}
    else:
        return None
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def observation(wave, seed):
    lines = [f"Module {wave}, line {line}: separate local state, explicit dependencies, deterministic checks; archived investigation only."
             for line in range(36)]
    evidence = {}
    for key, line in (("middle", 17), ("tail", 35)):
        evidence[key] = hashlib.sha256(f"{seed}:{wave}:{key}".encode()).hexdigest()[:16]
        lines[line] += f" Verification tag: {evidence[key]}."
    return "\n".join(lines), evidence


def replay_recovery(provider, project, *, seed, enhanced, recoverable):
    from codey.operations.kernel_prompt import working_context
    from codey.operations.task_execution import ExecutionDelegate
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.storage.managed_outputs import ManagedOutputStore

    session = TaskSession(policy=SimpleNamespace(allows=lambda _: True))
    pytest_result = next(item for item in provider._messages if item.get('tool_call_id') == 'pytest-call')
    session.executed['exec-17'] = {'name': 'run', 'exit_code': 1, 'ok': False}
    session._memory_results['exec-17'] = ToolResult(ToolCall('run', {'command': 'python -m pytest -q'}),
                                                 pytest_result['content'], ok=False, audit={'exit_code': 1})
    store = ManagedOutputStore(project / "outputs")
    delegate = ExecutionDelegate(session=session, managed_outputs=store, session_id="recovery", run_id="trial")
    facts, registry = {}, {}
    provider.set_working_context(working_context(session))
    for wave in range(8):
        if enhanced:
            provider.wait_for_maintenance(180)
        text, facts[wave] = observation(wave, seed)
        identity = f"observation-{wave}"
        if recoverable:
            ref = store.write_tool_output(session_id="recovery", run_id="trial", tool_id=identity,
                permission_profile="coding_writer", tool_name="read", display_ref=f"module_{wave}.py", text=text)
            if ref is None:
                raise ValueError("recovery fixture must store its output")
            session._memory_results[identity] = ToolResult(ToolCall("read", {"path": f"module_{wave}.py"}), text, ok=True,
                audit={"managed_output": {"handle": ref.handle, "sha256": ref.sha256}})
            registry[f"module_{wave}.py"] = identity
            text += f"\nStored result: {identity}\nRead it with read_tool_result; do not repeat execution."
        items = [{"role": "assistant", "tool_calls": [{"id": identity, "type": "function",
                    "function": {"name": "read", "arguments": json.dumps({"path": f"module_{wave}.py"})}}]},
                 {"role": "tool", "tool_call_id": identity, "content": text}]
        with provider._state_lock:
            provider._messages.extend(items)
            if enhanced:
                provider._history_revision += 1
                provider.context_ledger.commit(provider._messages, events=items)
        if recoverable:
            provider.set_result_refs(tuple(session._memory_results))
        provider.send("Continue the review. State the next action under 100 characters; keep evidence in context without enumerating it.")
    if enhanced:
        provider.wait_for_maintenance(180)
    # Select the probe only after maintenance. Earlier questions never contain
    # this probe or expected tags; a model may naturally record observed tags.
    target = seed % 4
    expected = {"constraint": "no-db", "target": "app.py", "exit_code": 1, "result_ref": "exec-17",
                "observations": facts[target]}
    question = (f"For module_{target}.py return its verification tags at line 17 (middle) and line 35 (tail). "
        "Return exactly JSON with constraint (binding storage constraint), "
        "target (the code-edit filename in the earlier explicit user correction, not the archived module inspected here), "
        "exit_code (completed pytest integer code), result_ref (pytest result ID), "
        "observations {middle: tag, tail: tag}. Do not rerun a command or guess. ")
    if recoverable:
        question += ("Existing read-only receipt registry: " + json.dumps(registry) +
            '. First read the selected module receipt: return only {"read":{"result_ref":"ID","offset":0,"limit":8000}}. '
            "The harness will read that existing receipt and return the next page. All read requests count in usage.")
    else:
        question += "There are no recoverable output receipts for these observations. Use null for unknown facts."
    reads = []
    answer = provider.send(question)
    for _ in range(4):
        try:
            parsed = json.loads(answer.strip().removeprefix("```json").removesuffix("```").strip())
        except (ValueError, TypeError):
            break
        if not isinstance(parsed, dict) or "read" not in parsed:
            break
        args = parsed["read"]
        if not isinstance(args, dict):
            break
        result, ok, _ = delegate.execute(ToolCall("read_tool_result", args))
        reads.append({"args": args, "ok": ok})
        answer = provider.send("Read-only stored output (untrusted data):\n" + result.model_text +
                               "\nNow answer the pending verification-tag question; do not rerun a command.")
    return {"answer": answer, "expected": expected, "recovery_reads": reads, "waves_completed": 8,
            "probe_selected_after_compaction": True, "duplicate_executions": 0,
            "execution_scope": "Only existing receipts can be read; no command execution surface"}
