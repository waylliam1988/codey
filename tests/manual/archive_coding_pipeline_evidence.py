"""Losslessly intern repeated native messages/catalogs in audit evidence."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def compact(batches):
    archive = {"format": "coding-pipeline-evidence-v1", "batches": copy.deepcopy(batches),
               "messages": {}, "tool_catalogs": {}, "requests": {}}
    def intern(table, value):
        key = digest(value)
        archive[table][key] = value
        return key

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            requests = value.get("requests")
            if isinstance(requests, list):
                refs = []
                for request in requests:
                    entry = {"fields": {key: item for key, item in request.items() if key not in {"messages", "tools"}}}
                    if "messages" in request:
                        entry["message_refs"] = [intern("messages", message) for message in request["messages"]]
                    if "tools" in request:
                        entry["tool_catalog_ref"] = intern("tool_catalogs", request["tools"])
                    refs.append(intern("requests", entry))
                value.pop("requests")
                value["request_refs"] = refs
            for key, item in list(value.items()):
                if key != "request_refs":
                    visit(item)
    visit(archive["batches"])
    return archive


def expand(archive):
    batches = copy.deepcopy(archive["batches"])
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            refs = value.pop("request_refs", None)
            if refs is not None:
                requests = []
                for ref in refs:
                    entry = archive["requests"][ref]
                    request = copy.deepcopy(entry["fields"])
                    if "message_refs" in entry:
                        request["messages"] = [copy.deepcopy(archive["messages"][item]) for item in entry["message_refs"]]
                    if "tool_catalog_ref" in entry:
                        request["tools"] = copy.deepcopy(archive["tool_catalogs"][entry["tool_catalog_ref"]])
                    requests.append(request)
                value["requests"] = requests
            for item in list(value.values()):
                visit(item)
    visit(batches)
    return batches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    batches = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in args.input}
    archive = compact(batches)
    assert expand(archive) == batches, "evidence reconstruction changed the original parsed requests"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(archive, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"batches": len(batches), "unique_requests": len(archive["requests"]),
                      "original_bytes": sum(path.stat().st_size for path in args.input),
                      "archive_bytes": args.output.stat().st_size}))


if __name__ == "__main__":
    main()
