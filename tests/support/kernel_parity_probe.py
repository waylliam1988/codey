"""Independent process probe; imports exclusively from the selected source tree.

Run by tools/kernel_parity.py with -I. No legacy implementation is copied here.
Only provider/network and verification subprocess results are deterministic fakes;
the selected revision's parsers, loops, guards and file tools execute for real.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


@contextlib.contextmanager
def native_tools_mode(enabled):
    previous = os.environ.get("NATIVE_TOOLS")
    os.environ["NATIVE_TOOLS"] = "1" if enabled else "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("NATIVE_TOOLS", None)
        else:
            os.environ["NATIVE_TOOLS"] = previous


def inventory(root):
    from codey.agents.request import AgentRequest
    from codey.policies.permissions import PERMISSION_PROFILES, allowed_coding_tool_names
    from codey.providers.registry import PROVIDER_LABELS
    from codey.research.tool_contract import TOOL_CONTRACTS
    from codey.toolchain.definition import TOOL_DEFINITIONS

    return {
        "coding": [dataclasses.asdict(row) for row in TOOL_DEFINITIONS],
        "research": [{"name": name, "required": list(row.required),
                      "optional": {key: {"type": arg.type.__name__, "default": arg.default}
                                   for key, arg in row.optional.items()},
                      "example": row.example} for name, row in TOOL_CONTRACTS.items()],
        "profiles": {name: {"coding": sorted(allowed_coding_tool_names(row)),
                            "research": list(row.research_tools),
                            "context": list(row.context_sources)}
                     for name, row in PERMISSION_PROFILES.items()},
        "request_fields": [row.name for row in dataclasses.fields(AgentRequest)],
        "providers": sorted(PROVIDER_LABELS),
        "source_hashes": {str(path.relative_to(root)).replace("\\", "/"):
                          hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
                          for path in sorted((root / "codey").rglob("*.py"))},
        "ast_surface": ast_surface(root),
    }


def ast_surface(root):
    result = {}
    for path in sorted((root / "codey").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        declarations = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                declarations.append({"kind": type(node).__name__, "name": node.name,
                                     "calls": sorted({ast.unparse(call.func) for call in ast.walk(node)
                                                      if isinstance(call, ast.Call)})})
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                declarations.extend({"kind": "Assign", "name": ast.unparse(target)} for target in targets)
        result[path.relative_to(root).as_posix()] = declarations
    return result


def protocol(case, legacy):
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.toolchain.definition import TOOL_DEFINITIONS

    reply = case["reply"]
    if case["wire"] == "native":
        reply = AssistantTurn(text=case.get("text", ""), tool_calls=tuple(
            ProviderToolCall(id=row["id"], name=row["name"], arguments=row["args"])
            for row in reply))
    if legacy:
        if case["domain"] == "research":
            from codey.research.protocols import JsonToolCodec
            codec = JsonToolCodec()
        elif case["wire"] == "native":
            from codey.protocols.native_openai import NativeOpenAIToolCodec
            codec = NativeOpenAIToolCodec(permission_profile=case["profile"])
        else:
            from codey.protocols.json_codec import JsonToolCodec
            codec = JsonToolCodec(permission_profile=case["profile"])
        plan = codec.parse_turn(reply) if case["wire"] == "native" else codec.parse(reply)
    else:
        from codey.operations.kernel_protocol import normalize_turn
        from codey.policies.task_policy import TaskPolicy
        grants = {"control", "project.read"}
        if case["domain"] == "research":
            grants = {"control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link"}
        elif case["profile"] == "coding_writer":
            grants |= {"project.write", "project.verify", "shell.approval"}
        plan = normalize_turn(reply, policy=TaskPolicy(grants=frozenset(grants)))
    aliases = {alias: row.name for row in TOOL_DEFINITIONS for alias in (row.runtime_name, *row.aliases) if alias}
    from codey.research.tool_contract import TOOL_CONTRACTS

    calls = []
    for call in plan.calls:
        name = aliases.get(call.name, call.name)
        args = dict(call.args)
        # An omitted optional default and an explicit default are equivalent.
        # Only defaults defined by the selected research contract are removed.
        contract = TOOL_CONTRACTS.get(name) if case["domain"] == "research" else None
        if contract:
            for key, arg in contract.optional.items():
                if args.get(key) == arg.default:
                    args.pop(key, None)
        calls.append({"name": name, "args": args, "id": call.call_id})
    done = plan.control if getattr(plan.control, "kind", "") == "done" else None
    return {"accepted": not bool(plan.protocol_error), "calls": calls,
            "done": done.body if done is not None else None}


class ScriptedProvider:
    name = "parity"
    location = "fake://parity"

    def __init__(self, case):
        self.case = case
        self.replies = iter(case["replies"])
        self.prompts = []
        self.receipts = []
        self.fresh = 0

    def new_chat(self, **_kwargs):
        self.fresh += 1
        if self.case.get("fresh_failure"):
            raise RuntimeError("fixed fresh failure")

    def send(self, text, **_kwargs):
        self.prompts.append(text)
        if self.case.get("send_failure"):
            raise RuntimeError("fixed send failure")
        reply = next(self.replies, None)
        if reply is None:
            raise RuntimeError(f"parity provider script exhausted: {self.case['id']}")
        if self.case.get("cancel_after_send"):
            self.stop_flag.set()
        return reply

    def send_turn(self, text, _tools, **_kwargs):
        from codey.providers.base import AssistantTurn, ProviderToolCall
        value = json.loads(self.send(text))
        return AssistantTurn(text="", tool_calls=(ProviderToolCall(
            id=f"call-{len(self.prompts)}", name=value["tool"], arguments=value["args"]),))

    def send_tool_results(self, messages, tools, **_kwargs):
        from codey.providers.base import AssistantTurn
        self.receipts.extend(row["tool_call_id"] for row in messages)
        if any(row["content"].startswith("OK:") or "budget exhausted" in row["content"] for row in messages):
            return AssistantTurn(text="closed", tool_calls=())
        return self.send_turn("native results", tools)


def coding_loop(case, legacy):
    import threading

    from codey.agents.handoff import ConversationContext
    from codey.agents.request import AgentRequest
    from codey.agents.tools import AgentToolFns
    from codey.completion.verification_policy import VerificationCandidate
    from codey.toolchain.runtime import ToolOutcome

    if legacy:
        from codey.agents.runner import run
    else:
        from codey.operations.project_adapter import run
    operations = []
    defaults = AgentToolFns()

    def wrap(name):
        fn = getattr(defaults, name)

        def call(*args, **kwargs):
            operations.append({"tool": name, "args": [str(item) for item in args[1:]], "kwargs": kwargs})
            return fn(*args, **kwargs)
        return call

    def check(_root, rel, command, **_kwargs):
        operations.append({"tool": "run_command", "args": [rel, command], "kwargs": {}})
        code = case.get("exit_code", 0)
        return ToolOutcome(f"exit code: {code}", code == 0, audit={"exit_code": code,
                           "command": command, "path": rel})

    tool_fns = dataclasses.replace(defaults, **{name: wrap(name) for name in (
        "read_file", "list_directory", "search_files", "find_references", "write_file", "edit_file")},
        run_command=check)
    with tempfile.TemporaryDirectory(prefix="codey-parity-project-") as td:
        root = Path(td)
        for name, content in case.get("files", {"a.py": "x = 1\n", "b.py": "y = 2\n"}).items():
            (root / name).write_text(content, encoding="utf-8")
        provider = ScriptedProvider(case)
        flag = threading.Event()
        provider.stop_flag = flag
        if case.get("pre_cancelled"):
            flag.set()
        conversation = ConversationContext() if case.get("conversation") else None
        events = []
        candidates = (VerificationCandidate(command="python -m pytest -q", cwd=".", source="parity"),)
        kwargs = {}
        if case.get("candidates"):
            kwargs["verification_candidates"] = candidates
        if case.get("candidate_loader"):
            def load():
                operations.append({"tool": "candidate_loader", "args": [], "kwargs": {}})
                return candidates
            kwargs["verification_candidate_loader"] = load
        result = None
        error = ""
        with native_tools_mode(case.get("native")):
            try:
                result = run(AgentRequest(
                    provider=provider, project=root, task=case.get("task", "Inspect the project"),
                    max_turns=case.get("max_turns", 5), stagnant_turns=case.get("stagnant_turns", 2),
                    provider_id="local" if case.get("native") else "",
                    on_event=events.append, stop_flag=flag, tool_fns=tool_fns, conversation=conversation,
                    fresh_chat=case.get("fresh_chat", True), strict_fresh_chat=case.get("strict_fresh", False),
                    permission_profile=case.get("profile", "coding_writer"), handoff=case.get("handoff", ""),
                    **kwargs,
                ))
            except Exception as exc:
                error = type(exc).__name__
        snapshot = conversation.snapshot if conversation else None
        return {"result": {key: getattr(result, key) for key in (
                    "stop_reason", "turns", "checks_passed", "checks_ran", "changed")} if result else None,
                "error": error, "operations": operations,
                "files": {path.name: path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py"))},
                "fresh": provider.fresh,
                "handoff_visible": not case.get("handoff") or any(case["handoff"] in p for p in provider.prompts),
                "snapshot": {"summary": snapshot.summary, "changed_files": list(snapshot.changed_files)}
                            if snapshot else None,
                "receipt_ids": provider.receipts}


def research_loop(case, legacy):
    import threading
    from types import SimpleNamespace

    from codey.knowledge.store import KnowledgeStore

    operations = []

    class Search:
        def search(self, query, limit=8):
            operations.append(["search", query])
            return [{"title": "Helium article", "url": "https://example.com/helium", "snippet": "Helium supply."}]

        def fetch(self, url):
            operations.append(["fetch", url])
            return {"url": url, "title": "Helium article", "text": "Helium is separated from natural gas streams.",
                    "truncated": False}

    replies = case["replies"] if legacy else case.get("current_replies", case["replies"])
    provider = ScriptedProvider(dict(case, replies=replies))
    flag = threading.Event()
    provider.stop_flag = flag
    with tempfile.TemporaryDirectory(prefix="codey-parity-research-") as td:
        store = KnowledgeStore(Path(td) / "knowledge")
        try:
            with native_tools_mode(case.get("native")):
                if legacy:
                    from codey.research.runner import ResearchRunner

                    runner = ResearchRunner(provider, Search(), store, max_turns=case.get("max_turns", 6),
                                            controller_enabled=case.get("controller", True), session_id="s", project="")
                    list(runner.run("How is helium obtained?"))
                    result = runner.result
                    tools = runner.tools
                else:
                    from codey.operations.research_iteration import run_research_iteration

                    iteration = run_research_iteration(SimpleNamespace(knowledge_store=store), provider=provider,
                        session_id="s", project="", task="How is helium obtained?", max_turns=case.get("max_turns", 6),
                        on_event=lambda _e: None, stop_flag=flag, provider_id="local", run_id="", chat_handoff="",
                        trace_recorder=None, search=Search())
                    result = iteration.result
                    tools = iteration.tools
            # Exclude generated ids and timestamps, retain note contents and
            # source/evidence facts. A note disappearing is never normalized away.
            notes = [store.read_note(key) for key in tools.created_ids]
            return {"operations": operations, "stop_reason": result.stop_reason,
                    "source_urls": sorted(tools.sources_read), "evidence_count": len(tools.ledger.evidence_items),
                    "notes": [{
                        "type": note.type, "title": note.title, "body": note.body,
                        "tags": list(note.tags), "sources": list(note.sources),
                        "aliases": list(note.aliases), "relations": [dict(row) for row in note.relations],
                        "open_questions": list(note.open_questions), "confidence": note.confidence,
                        "status": note.status, "session_id": note.session_id, "project": note.project,
                        "valid_until": note.valid_until,
                    } for note in notes if note is not None]}
        finally:
            store.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--inventory", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    import codey
    if Path(codey.__file__).resolve().parent != args.source.resolve() / "codey":
        raise RuntimeError("wrong source tree imported")
    if args.inventory:
        payload = inventory(args.source)
    else:
        cases = json.load(sys.stdin)
        handlers = {"protocol": protocol, "coding_loop": coding_loop, "research_loop": research_loop}
        payload = {case["id"]: handlers[case["boundary"]](case, args.legacy) for case in cases}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
