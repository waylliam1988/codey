"""Finite, stable partitions derived from the frozen *legacy* tool inventory."""
from __future__ import annotations

import copy
import json


def json_call(tool, **args):
    return json.dumps({"tool": tool, "args": args}, ensure_ascii=False)


def protocol_cases(inventory):
    rows = []

    def add(domain, profile, wire, label, value, **extra):
        rows.append({"id": f"protocol/{domain}/{profile}/{wire}/{label}", "boundary": "protocol",
                     "domain": domain, "profile": profile, "wire": wire, "reply": value, **extra})

    for domain in ("coding", "research"):
        definitions = inventory[domain]
        profiles = ("coding_writer", "planning_readonly") if domain == "coding" else ("research",)
        for profile in profiles:
            for definition in definitions:
                examples = definition.get("examples") or [definition["example"]]
                for i, example in enumerate(examples):
                    obj = json.loads(example)
                    name = definition["name"]
                    add(domain, profile, "json", f"{name}/example-{i}", example)
                    # Native batch wrappers did not exist in the legacy codec.
                    if domain == "research" or definition.get("runtime_name") is not None or name == "done":
                        native_name = definition.get("runtime_name") or name
                        add(domain, profile, "native", f"{name}/example-{i}",
                            [{"id": "c1", "name": native_name, "args": obj["args"]}])
                    if i:
                        continue
                    for alias in definition.get("aliases", []):
                        alias_obj = dict(obj, tool=alias)
                        add(domain, profile, "json", f"{name}/alias-{alias}", json.dumps(alias_obj))
                    for required in definition["required"]:
                        args = dict(obj["args"])
                        args.pop(required, None)
                        add(domain, profile, "json", f"{name}/missing-{required}", json_call(name, **args))
                    # One-field mutations cover each declared argument, including
                    # legacy coercion and optional defaults; no random sampling.
                    params = (dict(definition["parameters"]) if domain == "coding"
                              else {**{k: {"type": "string"} for k in definition["required"]},
                                    **definition["optional"]})
                    for key in params:
                        for label, value in (("null", None), ("bool", True), ("object", {}),
                                             ("empty", ""), ("number", 2), ("numeric-text", "2")):
                            args = dict(obj["args"], **{key: value})
                            add(domain, profile, "json", f"{name}/{key}-{label}", json_call(name, **args))
                    add(domain, profile, "json", f"{name}/extra-key", json_call(name, **obj["args"], surprise="x"))
            read_name = "read_file" if domain == "coding" else "web_search"
            read_args = {"path": "a.py"} if domain == "coding" else {"query": "helium"}
            valid = json_call(read_name, **read_args)
            envelopes = {"fenced": f"```json\n{valid}\n```", "prose": f"Please execute {valid}",
                         "think": f"<think>consider alternatives</think>{valid}",
                         "unknown-template": f"<alien_call>{valid}</alien_call>",
                         "truncated": valid[:-1], "plain": "I have completed the task", "empty": "",
                         "unknown-tool": json_call("invented_tool", path="a.py"),
                         "adjacent": valid + "\n" + valid,
                         "done-answer": json_call("done", answer="legacy answer"),
                         "done-summary": json_call("done", summary="canonical answer"),
                         "done-nested": json_call("done", summary=valid),
                         "name-flat": json.dumps({"name": read_name, **read_args}),
                         "args-array": json.dumps({"tool": read_name, "args": []})}
            for label, reply in envelopes.items():
                add(domain, profile, "json", f"envelope/{label}", reply)
            for size in (0, 1, 4, 8, 9):
                add(domain, profile, "native", f"batch/{size}",
                    [{"id": f"c{i}", "name": read_name, "args": read_args} for i in range(size)], text=valid)
            add(domain, profile, "native", "batch/missing-id", [{"id": "", "name": read_name, "args": read_args}])
            add(domain, profile, "native", "batch/mixed-done", [
                {"id": "c1", "name": read_name, "args": read_args},
                {"id": "c2", "name": "done", "args": {"summary": "finished"}}])
    # Valid canonical project argument aliases that must remain supported.
    for tool, args in (("list_dir", {"cwd": "."}), ("grep", {"pattern": "x", "cwd": "."}),
                       ("find_references", {"name": "x"}), ("run", {"cmd": "python -m pytest -q", "cwd": "."}),
                       ("edit", {"path": "a.py", "old": "x", "new": "y"}),
                       ("read_files", {"paths": "a.py"})):
        add("coding", "coding_writer", "json", f"repair/{tool}", json_call(tool, **args))
    return rows


def loop_cases():
    done = json_call("done", summary="finished")
    read = json_call("read_file", path="a.py")
    edit = json_call("edit", path="a.py", old_string="x = 1", new_string="x = 3")
    create = json_call("edit", path="new.py", content="z = 4\n")
    run = json_call("run", command="python -m pytest -q", path=".")
    cases = [
        ("read-done", [read, done], {}),
        ("create-done", [create, done], {}),
        ("read-edit-check-done", [read, edit, run, done], {}),
        ("read-edit-no-check", [read, edit, done, run, done], {}),
        ("verification-forbidden", [read, edit, done], {"task": "Change x; do not run tests"}),
        ("verification-requested", [read, edit, done, run, done], {"task": "Change x and run tests"}),
        ("trusted-candidate", [read, edit, done, run, done], {"candidates": True}),
        ("candidate-loader", [read, edit, done, run, done], {"candidate_loader": True}),
        ("failed-check", [read, edit, run, done], {"exit_code": 1}),
        ("read-before-edit", [edit, done], {}),
        ("path-traversal", [json_call("read_file", path="../outside.py"), done], {}),
        ("repeat-information", [read] * 6, {"max_turns": 6}),
        ("invalid-stop", ["no tools"] * 6, {"max_turns": 6}),
        ("readonly-denies-edit", [create, done], {"profile": "planning_readonly"}),
        ("batch-read", [json_call("read_files", paths=["a.py", "b.py"]), done], {}),
        ("parallel-read", [json_call("parallel", calls=[json.loads(read),
                          {"tool": "list_dir", "args": {"path": "."}}]), done], {}),
        ("conversation", [read, edit, run, done], {"conversation": True}),
        ("handoff", [read, done], {"handoff": "PARITY_PRIOR_WORK"}),
        ("reuse-chat", [read, done], {"fresh_chat": False}),
        ("fresh-failure", [read, done], {"fresh_failure": True}),
        ("strict-fresh-failure", [read, done], {"fresh_failure": True, "strict_fresh": True}),
        ("provider-failure", [read], {"send_failure": True}),
        ("pre-cancelled", [read], {"pre_cancelled": True}),
        ("cancel-after-send", [create, done], {"cancel_after_send": True}),
        ("native-read-done", [read, done], {"native": True}),
        ("native-budget", [read], {"native": True, "max_turns": 1}),
    ]
    return [{"id": f"loop/{name}", "boundary": "coding_loop", "replies": replies, **opts}
            for name, replies, opts in cases]


def cases_for_inventory(inventory):
    cases = protocol_cases(copy.deepcopy(inventory)) + loop_cases() + research_cases()
    assert len({row["id"] for row in cases}) == len(cases)
    return cases


def research_cases():
    recall = json_call("knowledge_search", query="helium")
    search = json_call("web_search", query="helium production")
    opened = json_call("open_url", url="https://example.com/helium")
    note = json_call("knowledge_write", type="fact", title="Helium production",
                     body="Helium is separated from natural gas streams.", sources=["https://example.com/helium"],
                     evidence=[{"claim": "Helium production", "source_url": "https://example.com/helium",
                                "excerpt": "Helium is separated from natural gas streams.", "stance": "supports"}])
    report = ("## 结论\n- Helium supply depends on gas processing. [1]\n\n"
              "## 关键证据\n- [1] The opened source says helium is separated from natural gas streams.\n\n"
              "## 反证与限制\n- 未找到强反证；需要持续追踪新供应数据。\n\n"
              "## 来源质量\n- [1] secondary · web · fresh · example.com\n\n"
              "## 搜索覆盖\n- query: helium production\n- opened: Helium article\n- skipped: none representative\n\n"
              "## 来源\n[1] Helium article - https://example.com/helium")
    done = json_call("done", answer=report)
    source_note = json.loads(note)
    source_note["args"]["sources"] = ["s1"]
    source_note["args"]["evidence"][0]["source_url"] = "s1"
    cases = [
        ("evidence-synthesis", [recall, search, opened, note, done], {"controller": False}),
        ("synthesis-questions", [recall, search, opened, note,
                               json_call("done", answer=report, open_questions=["Other helium sources?"])], {"controller": False}),
        ("controller-open-result", [recall, search, json_call("open_result", result_id="r1"), note, done], {}),
        ("source-search", [recall, search, opened, json_call("source_search", url="https://example.com/helium",
                                                         query="natural gas"), note, done], {"max_turns": 7, "controller": False}),
        ("search-only-budget", [recall, search], {"max_turns": 2}),
        ("knowledge-read-budget", [json_call("knowledge_read", id="missing")], {"max_turns": 1}),
        ("native-evidence-synthesis", [recall, search, opened, note, done], {"native": True}),
        ("controller-source-references", [recall, search, json_call("open_result", result_id="r1"),
                                           json.dumps(source_note), done], {}),
        ("controller-reopen-source", [recall, search, json_call("open_result", result_id="r1"),
                                      json_call("reopen_source", source_id="s1"), note, done], {"max_turns": 7}),
        ("source-hit", [recall, search, opened, json_call("source_search", source_id="s1", query="natural gas"),
                        json_call("open_hit", hit_id="h1"), note, done], {"max_turns": 8, "controller": False}),
    ]
    rows = []
    for name, replies, opts in cases:
        current = []
        for reply in replies:
            obj = json.loads(reply)
            if obj["tool"] == "done":
                obj["args"]["summary"] = obj["args"].pop("answer")
            current.append(json.dumps(obj, ensure_ascii=False))
        rows.append({"id": f"research/{name}", "boundary": "research_loop", "replies": replies,
                     "current_replies": current, **opts})
    return rows
