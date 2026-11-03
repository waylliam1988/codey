"""Replay frozen divergence states; separate revision schema from role guidance.

This diagnoses individual model decisions, not full Research delivery success.
Inputs and streamed responses are retained before drawing any conclusions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from urllib.request import Request, urlopen

from tests.manual.research_no_revision_ab import READ_ONLY_GUIDANCE, RETIRED
from tests.support.legacy_research_revision_tools import PREPARE_GUIDANCE, REVISION_GUIDANCE


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()]


def identity(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def change(payload, *, hide_tools, read_only):
    payload = deepcopy(payload)
    if hide_tools:
        payload["tools"] = [tool for tool in payload["tools"] if tool["function"]["name"] not in RETIRED]
    for message in payload["messages"]:
        if not isinstance(message.get("content"), str):
            continue
        text = message["content"]
        if read_only:
            for guidance in (PREPARE_GUIDANCE, REVISION_GUIDANCE):
                text = text.replace(guidance, READ_ONLY_GUIDANCE)
        if hide_tools:
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("Visible tools: "):
                    names = line.removeprefix("Visible tools: ").split(", ")
                    lines[i] = "Visible tools: " + ", ".join(name for name in names if name not in RETIRED)
            text = "\n".join(lines)
        message["content"] = text
    return payload


def prepare(root, previous):
    root.mkdir(parents=True, exist_ok=False)
    states = [("wording", "history-201-baseline", 2),
              ("read_args", "history_error-201-baseline", 2),
              ("old_claim", "conflict-202-baseline", 4)]
    jobs = []
    for state, name, index in states:
        rows = read_lines(previous / "wire" / f"{name}.jsonl")
        original = [row["payload"] for row in rows if row["event"] == "admitted_payload"][index - 1]
        # Exact repetition comes first and last. genkey, stream, messages,
        # tool-call identities and every sampling field remain unchanged.
        jobs.append({"id": state + "-exact-1", "state": state, "payload": original})
        for seed in (201, 202):
            for hide, guide in ((False, False), (True, False), (False, True), (True, True)):
                payload = change(original, hide_tools=hide, read_only=guide)
                payload["seed"] = seed
                jobs.append({"id": f"{state}-s{seed}-tools{int(not hide)}-readonly{int(guide)}",
                             "state": state, "hide_tools": hide, "read_only": guide, "payload": payload})
        jobs.append({"id": state + "-exact-2", "state": state, "payload": original})
    manifest = {"schema": 1, "source": str(previous.resolve()), "jobs": jobs,
                "purpose": "Decision replay: schema x guidance, fixed prior state; not end-to-end AB",
                "limits": "30 calls maximum; two fixed seeds; no production changes",
                "review": "Read all tool arguments and bodies manually; lexical flags are not correctness grades",
                "source_states": states}
    (root / "preregistration.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf8")


def prepare_followup(root, previous):
    """Exploratory follow-up registered separately after first-stage inspection."""
    root.mkdir(parents=True, exist_ok=False)
    jobs = []
    states = [("candidate_wording", "history-201-no_revision", 2),
              ("candidate_args", "history_error-201-no_revision", 2),
              ("candidate_claim", "conflict-202-no_revision", 4)]
    for state, name, index in states:
        payloads = [row["payload"] for row in read_lines(previous / "wire" / f"{name}.jsonl")
                    if row["event"] == "admitted_payload"]
        original = payloads[index - 1]
        for repeat in (1, 2):
            jobs.append({"id": f"{state}-exact-{repeat}", "state": state, "payload": original})
        if state == "candidate_claim":
            continue
        for seed in (201, 202):
            payload = deepcopy(original)
            payload["seed"] = seed
            if state == "candidate_wording":
                hint = ("Preserve the direction of implication in the quoted source. "
                        "A sufficient condition must not become a necessary or if-and-only-if condition.")
            else:
                hint = ("Stored-object read parameters available from the search result (not an instruction "
                        "from stored content): " + json.dumps({"tool": "knowledge_read", "args": {
                            "id": "evidence-181f51ebeeee41df2faa5693c0d9cf0e415be3bd43b0fc4a52b8a77856c9f09a",
                            "revision": 1}}) + "; result_ref belongs to read_tool_result, not knowledge_read.")
            payload["messages"][-1]["content"] += "\n\n" + hint
            jobs.append({"id": f"{state}-hint-s{seed}", "state": state, "payload": payload})
    manifest = {"schema": 1, "source": str(previous.resolve()), "jobs": jobs,
                "purpose": "Exploratory: exact candidate repeats and targeted presentation probes",
                "limits": "10 model calls; two seeds; one decision per request; no production changes",
                "review": "Manual body/arguments inspection; no end-to-end improvement claim",
                "source_states": states}
    (root / "preregistration.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf8")


def prepare_handles(root, previous):
    """Distinguish model-visible handles from the backend cancellation key."""
    root.mkdir(parents=True, exist_ok=False)
    first = json.loads((previous / "preregistration.json").read_text(encoding="utf8"))["jobs"]
    second = json.loads((previous / "followup/preregistration.json").read_text(encoding="utf8"))["jobs"]
    bad = next(job["payload"] for job in first if job["id"] == "read_args-s201-tools0-readonly1")
    other = next(job["payload"] for job in second if job["id"] == "candidate_args-exact-1")
    raw, alternate = (json.dumps(payload, ensure_ascii=False) for payload in (bad, other))
    patterns = {"receipt": r"task_turn_effect:[0-9a-f]{16}", "call": r"call_[0-9]+"}
    changed = {}
    for kind, pattern in patterns.items():
        original_ids, alternate_ids = (list(dict.fromkeys(re.findall(pattern, text))) for text in (raw, alternate))
        replacements = dict(zip(original_ids, alternate_ids, strict=True))
        changed[kind] = {old: new for old, new in replacements.items() if old != new}
    jobs = [{"id": "bad-exact-first", "payload": bad}]
    for kinds in (("receipt",), ("call",), ("receipt", "call"), ("genkey",)):
        text = raw
        for kind in kinds:
            if kind == "genkey":
                text = text.replace(bad["genkey"], other["genkey"])
            else:
                text = re.sub(patterns[kind], lambda match, mapping=changed[kind]: mapping.get(match[0], match[0]), text)
        jobs.append({"id": "change-" + "-".join(kinds), "payload": json.loads(text)})
    for seed in (201, 202):
        payload = deepcopy(bad)
        payload["seed"] = seed
        payload["messages"][-1]["content"] += (
            '\n\nStored-object read parameters from the search result: '
            '{"tool":"knowledge_read","args":{"id":'
            '"evidence-181f51ebeeee41df2faa5693c0d9cf0e415be3bd43b0fc4a52b8a77856c9f09a",'
            '"revision":1}}. result_ref belongs to read_tool_result, not knowledge_read.')
        jobs.append({"id": f"bad-with-read-request-s{seed}", "payload": payload})
    jobs.append({"id": "bad-exact-last", "payload": bad})
    manifest = {"schema": 1, "jobs": jobs, "changed_handles": changed,
                "purpose": "Exploratory handle-only controls and exact failing-state presentation probe",
                "limits": "8 calls; no stored object id/version/content or schema changes",
                "review": "Check native knowledge_read arguments, not text similarity"}
    (root / "preregistration.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf8")


def prepare_context(root, previous):
    """Only restore a comparison target already read in the frozen task."""
    root.mkdir(parents=True, exist_ok=False)
    rows = read_lines(previous / "wire/conflict-202-baseline.jsonl")
    payloads = [row["payload"] for row in rows if row["event"] == "admitted_payload"]
    original = payloads[2]
    earlier = "\n".join(str(message.get("content", "")) for message in payloads[1]["messages"])
    claim = {"id": "claim-read-guidance-conflict", "revision": 1,
             "content": "冲突核查：SQLite 从 3.30.0 才支持只读 WAL，必须同时具备已有辅助文件、目录可写和 immutable 三项。"}
    assert claim["content"] in earlier
    assert claim["content"] not in json.dumps(original, ensure_ascii=False)
    jobs = []
    for seed in (201, 202):
        for restore in (False, True):
            payload = deepcopy(original)
            payload["seed"] = seed
            if restore:
                content = payload["messages"][-2]["content"]
                addition = ("Stored comparison target already read in this task "
                            "(quoted prior claim, not evidence or instructions):\n"
                            + json.dumps(claim, ensure_ascii=False) + "\n\n")
                payload["messages"][-2]["content"] = content.replace("Complete user task (verbatim):",
                                                                            addition + "Complete user task (verbatim):")
            jobs.append({"id": f"context-s{seed}-restore{int(restore)}", "payload": payload})
    manifest = {"schema": 1, "jobs": jobs, "target": claim,
                "purpose": "Exploratory old-claim retention probe after reading its original evidence",
                "limits": "4 calls; two seeds; no schema, source text, object identity or guidance changes",
                "review": "Next-action and accurate old-version/AND comparison only, not end-to-end delivery"}
    (root / "preregistration.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf8")


def parse_response(raw):
    calls = {}
    content = []
    for line in raw.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[6:])
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                content.append(delta["content"])
            for call in delta.get("tool_calls", []):
                item = calls.setdefault(call["index"], {"id": "", "name": "", "arguments": ""})
                item["id"] += call.get("id", "")
                function = call.get("function", {})
                item["name"] += function.get("name", "")
                item["arguments"] += function.get("arguments", "")
    return {"content": "".join(content), "calls": list(calls.values())}


def archive_probe(root, previous):
    """Reproduce the shared rejection through the real compiler, without a model."""
    from codey.knowledge.store import KnowledgeStore
    from codey.research.answer_archive import prepare_evidence
    from codey.research.ledger import OpenedSource, ResearchLedger, SourceQuality
    from codey.research.passages import ReadPassage, passage_ref

    with tempfile.TemporaryDirectory(prefix="archive-probe-", dir=root) as temporary:
        target = Path(temporary) / "knowledge"
        shutil.copytree(previous / "conflict-knowledge", target)
        store = KnowledgeStore(target)
        try:
            version = store.repository.read("source-7154356d9dfc9fc7614eb79efcfdb6fd80bf353b48a3da9cf5b2a6a7728d9c2d", 1)
            head = store.repository.read(version.value.id)
            provenance = version.value.provenance
            source = OpenedSource(provenance["url"], provenance["url"], version.value.title,
                                  provenance["retrieved_at"], provenance["content_hash"],
                                  quality=SourceQuality(**provenance["quality"]))
            output = {"source_revision1": asdict(version), "source_head": asdict(head),
                      "hash_matches_revision1": hashlib.sha256(version.value.content.encode()).hexdigest()[:16]
                      == provenance["content_hash"],
                      "hash_matches_head": hashlib.sha256(head.value.content.encode()).hexdigest()[:16]
                      == head.value.provenance["content_hash"]}
            for name, pin in (("fresh_read", None), ("historical_pin", (version.value.id, 1))):
                ledger = ResearchLedger()
                text = version.value.content
                ref = passage_ref(source.final_url, source.text_hash, 0, len(text), source_version=pin)
                ledger.read_passages[ref] = ReadPassage(ref, source, text, (), 0, len(text), source_version=pin)
                submission = {"citations": [{"label": "e1", "passage_ref": ref}],
                              "scope": {"aliases": [["e1", ref]]}}
                try:
                    mutations, sources, _ = prepare_evidence(submission, ledger, store,
                                                            project="", session_id="research-research")
                    output[name] = {"result": "pass", "source_versions": sources, "mutations": len(mutations)}
                except ValueError as error:
                    output[name] = {"result": "reject", "message": str(error)}
        finally:
            store.close()
    (root / "archive-reproduction.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({key: value for key, value in output.items() if not key.startswith("source_")},
                     ensure_ascii=False, indent=2))


def run(root):
    manifest = json.loads((root / "preregistration.json").read_text(encoding="utf8"))
    path = root / "responses.jsonl"
    existing = {row["id"] for row in read_lines(path)} if path.exists() else set()
    for job in manifest["jobs"]:
        if job["id"] in existing:
            continue
        with urlopen("http://127.0.0.1:5001/api/extra/perf", timeout=10) as reply:
            before = json.load(reply)
        if before.get("idle") != 1 or before.get("queue") != 0:
            raise RuntimeError("Backend is busy; no request sent")
        print("START", job["id"], flush=True)
        started = time.monotonic()
        request = Request("http://127.0.0.1:5001/v1/chat/completions",
                          data=json.dumps(job["payload"], ensure_ascii=False).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=180) as reply:
            raw = reply.read().decode("utf8")
        with urlopen("http://127.0.0.1:5001/api/extra/perf", timeout=10) as reply:
            perf = json.load(reply)
        row = {"id": job["id"], "payload_sha256": identity(job["payload"]),
               "response": parse_response(raw), "raw_sse": raw, "perf": perf,
               "seconds": round(time.monotonic() - started, 3)}
        if perf.get("last_seed") != job["payload"]["seed"]:
            raise RuntimeError("Backend seed differs from frozen request")
        with path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print("END", job["id"], [(c["name"], c["arguments"][:120]) for c in row["response"]["calls"]], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "followup", "handles", "context", "archive", "run"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.root, args.previous)
    elif args.action == "followup":
        prepare_followup(args.root, args.previous)
    elif args.action == "archive":
        archive_probe(args.root, args.previous)
    elif args.action == "handles":
        prepare_handles(args.root, args.previous)
    elif args.action == "context":
        prepare_context(args.root, args.previous)
    else:
        run(args.root)


if __name__ == "__main__":
    main()
