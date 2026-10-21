"""Measure source-commit races after real canonical auxiliary HTTP (offline)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO

sys.path.insert(0, str(REPO))

import protocol_source_lifetime as lifetime  # noqa: E402

from codey.durable import CommitConflict, DurableSession  # noqa: E402
from tests.manual.ghost_memory_routing.canonical_source_fence_report import (  # noqa: E402
    ARMS,
    CHANGES,
    SCHEMA,
    summarize,
)


class CommitObserved(Exception):
    """End a measurement after its source transaction, before any final task."""


def probe(directory: Path, *, protocol: str, arm: str, change: str) -> dict:
    original = DurableSession.admit_write
    observed = {}

    def intercept(durable, **kwargs):
        if kwargs.get("request_id") != "checkpoint-canonical-edit":
            return original(durable, **kwargs)
        head = kwargs["expected_head"]
        revision = kwargs["expected_revision"]
        source_id = kwargs["edits"][0]["target"]
        assert durable.head(lifetime.SESSION) == (head, revision)
        assert kwargs["data"]["source_entry_ids"][0] == source_id
        assert kwargs["data"]["captured_revision"] == revision
        prior_effects = [effect for task in range(1, lifetime.CHECKPOINT_PRELUDE_TASKS + 1)
                        for effect in durable.effects(f"source-run-{task}")]
        if (len(prior_effects) != 10 or any(effect.settlement is None for effect in prior_effects)):
            raise RuntimeError("missing successful task prelude settlements")
        store_commit = durable.store.commit
        competing = DurableSession(durable.store)
        injected = False

        def mutate():
            nonlocal injected
            injected = True
            if change.endswith("append"):
                competing.append_message(lifetime.SESSION, "user_message", {"text": "Synthetic concurrent tail."})
            elif change == "branch":
                competing.branch(lifetime.SESSION, source_id, expected_revision=revision)
            elif change == "edit":
                original(competing, conversation_id=lifetime.SESSION, request_id="competing-edit",
                         kind="context_edit", data={}, display_only=False,
                         edits=[{"target": source_id, "action": "omit"}])
            elif change.endswith("recreate"):
                competing.delete_conversation(lifetime.SESSION)
                competing.create_conversation(lifetime.SESSION)
                for _ in range(revision):
                    competing.append_message(lifetime.SESSION, "user_message", {"text": "Synthetic new session."})
                if competing.head(lifetime.SESSION)[1] != revision:
                    raise RuntimeError("recreate did not reproduce the source revision")
            else:
                raise ValueError(change)

        def commit(mutation):
            if mutation.submission is not None and mutation.submission.request_id == "checkpoint-canonical-edit":
                mutate()
            return store_commit(mutation)

        if arm == "unfenced":
            kwargs.pop("expected_revision")
            kwargs.pop("expected_head")
        elif arm == "revision":
            kwargs.pop("expected_head")
        elif arm != "head_revision":
            raise ValueError(arm)
        if change != "none" and not change.startswith("commit_"):
            mutate()
        accepted = False
        error = None
        before_sequence = durable.commit_sequence()
        with patch.object(durable.store, "commit", commit if change.startswith("commit_") else store_commit):
            try:
                original(durable, **kwargs)
            except CommitConflict as exc:
                error = type(exc).__name__
            else:
                accepted = True
        edits = [entry for entry in durable.entries(lifetime.SESSION)
                 if entry.kind == "context_edit" and entry.payload.get("data", {}).get("experiment") == "checkpoint-unification"]
        if bool(edits) != accepted or (change != "none") != injected:
            raise RuntimeError("commit evidence does not match observed transaction")
        if not accepted and not change.startswith("commit_") and durable.commit_sequence() != before_sequence:
            raise RuntimeError("a refused proposal wrote additional durable state")
        observed.update(protocol=protocol, arm=arm, change=change, accepted=accepted, error=error,
                        injected=injected, same_revision=(change.endswith("recreate")),
                        source_digest=kwargs["data"]["source_digest"],
                        source_entries=len(kwargs["data"]["source_entry_ids"]),
                        committed_edits=len(edits), prelude_tasks=5, prelude_settlements=len(prior_effects),
                        final_task_ran=False, final_hint_result=None)
        raise CommitObserved

    with patch.object(DurableSession, "admit_write", intercept):
        try:
            lifetime.probe(directory, protocol=protocol, lifetime="app_restart", revoke=False,
                           handoff_source="canonical_edit", checkpoint_mode="trimmed_replay")
        except CommitObserved:
            pass
        else:
            raise RuntimeError("canonical source transaction was not observed")
    payloads = json.loads((directory / "requests.json").read_text(encoding="utf-8"))
    auxiliary = [payload for payload in payloads
                 if any(value.startswith("<source>\n") for value in lifetime.strings(payload))]
    canonical = auxiliary[-1]
    if lifetime.contains(canonical) or lifetime.WORK_FACT not in lifetime.encode(canonical):
        raise RuntimeError("canonical source was contaminated or lost the early fact")
    main_http = len(payloads) - len(auxiliary)
    if main_http != 5 or len(auxiliary) < 1 or not observed:
        raise RuntimeError("incomplete actual HTTP prelude")
    observed.update(main_http=main_http, auxiliary_http=len(auxiliary),
                    canonical_source_has_fact=True, canonical_source_has_hint=False)
    return observed



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite")
    parser.add_argument("--metrics-out", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.report:
        print(json.dumps(summarize(json.loads(args.report.read_text(encoding="utf-8"))), indent=2))
        return
    if not args.suite or Path(args.suite).name != args.suite or args.suite in {".", ".."}:
        parser.error("--suite must be a directory name")
    root = DATA_ROOT / args.suite
    root.mkdir(parents=True, exist_ok=False)
    config = {"suite": args.suite, "model": lifetime.MODEL, "endpoint": "ephemeral loopback HTTP",
              "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
              "cells": 42, "reps": 1, "context": 16384, "reserve": 2048, "keep_recent": 1000,
              "output_tokens": 128, "temperature": 0, "timeout_seconds": 5, "max_turns": 1,
              "on_off": "source fence disabled / revision only / head and revision; prelude hints enabled",
              "counter": "ceil(serialized_chars/4), estimated", "stop": "after source transaction; no final task"}
    (root / "manifest.json").write_text(lifetime.encode({**config, "command": subprocess.list2cmdline(sys.argv),
        "started_at": datetime.now(UTC).isoformat()}), encoding="utf-8")
    rows = []
    for protocol in lifetime.PROTOCOLS:
        for arm in ARMS:
            for change in CHANGES:
                cell = f"{protocol}-{arm}-{change}"
                try:
                    row = probe(root / cell, protocol=protocol, arm=arm, change=change)
                except Exception as exc:
                    (root / "failure.json").write_text(lifetime.encode({"cell": cell, "error": repr(exc),
                        "at": datetime.now(UTC).isoformat()}), encoding="utf-8")
                    raise
                rows.append(row)
                print(json.dumps(row), flush=True)
    metrics = {"schema": SCHEMA, "config": config, "rows": rows}
    summary = summarize(metrics)
    (root / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if args.metrics_out:
        args.metrics_out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
