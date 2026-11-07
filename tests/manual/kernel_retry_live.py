"""Real local-model retry/restart probe with one explicit malformed-response injection.

Uses the production kernel, SQLite store, request freezer and provider. The
first genuine model response is recorded, then replaced with invalid JSON to
exercise the retry boundary. This is a transport fault injection, not a claim
that the model naturally failed its protocol.
"""
from __future__ import annotations

import argparse
import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations.kernel_driver import load_prepared_request
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig
from codey.providers.local_connection import connect_local
from tests.durable.test_operation_control_actions import request_for
from tests.durable.test_provider_response_receipts import runtime
from tests.durable.test_retry_observation_commit import reopen_retry
from tests.support.kernel_restart import SimulatedExit
from tools.local_model_gate_attempts import record_api_generations


def probe(directory, *, base_url, model, seed):
    directory.mkdir(parents=True, exist_ok=False)
    runtime_path = directory / "runtime"
    runtime_path.mkdir()
    config = LocalProviderConfig(base_url=base_url, model=model, api_key="local", native_tools_mode="json",
        context=LocalContextBudget(32768, 1024, 12000), thinking_enabled=False)
    task = "Call done with summary exactly RETRY-RESTORED-731. No external actions are required."
    policy = TaskPolicy(frozenset({"control"}))
    snapshots, invocations = [], []

    class ObservedProvider:
        def __init__(self, provider, inject):
            self.provider, self.inject = provider, inject

        def __getattr__(self, name):
            return getattr(self.provider, name)

        def send(self, prompt, **kwargs):
            original = self.provider.send(prompt, **kwargs)
            emitted = '{"tool": broken' if self.inject else original
            invocations.append({"injected": self.inject, "original": original, "emitted": emitted})
            self.inject = False
            return emitted

    def run(durable, sink, provider):
        request = request_for(durable, sink, provider)
        request = replace(request, transport=replace(request.transport, user_task=task, stagnant_turns=2))
        return run_task_kernel(TaskSession(policy, task_text=task, max_turns=4), request=request)

    def configure(provider):
        provider.temperature = 0.3
        original = provider.configure_request

        def sampling(payload):
            original(payload)
            payload["seed"] = seed
        provider.configure_request = sampling

    durable, _, _, sink = runtime(runtime_path, sqlite=True)

    def crash_after_observation(*_args):
        state = operation_from_checkpoint(durable.task("r"), "s")
        if state.leaf == "control_ready":
            artifact = durable.artifact(state.work_ref)
            envelope = sink.managed_outputs.read_json_artifact(dict(artifact.metadata["ref"]))
            assert state.counters.invalid_turns == 1
            assert envelope["schema_version"] == 2 and "counters" not in envelope["data"]
            snapshots.append({"leaf": state.leaf, "counters": state.counters.to_payload(), "decision": envelope})
            raise SimulatedExit()

    durable.subscribe_commits(crash_after_observation)
    with record_api_generations(directory / "provider.jsonl"):
        try:
            with closing(connect_local(config=config)) as provider:
                configure(provider)
                try:
                    run(durable, sink, ObservedProvider(provider, True))
                except SimulatedExit:
                    pass
                else:
                    raise AssertionError("retry boundary was not reached")
        finally:
            durable.close()
        durable, _, sink = reopen_retry(runtime_path)

        def observe_freeze(*_args):
            state = operation_from_checkpoint(durable.task("r"), "s")
            if (state.leaf == "provider_request_ready" and state.logical_turn == 2
                    and not any(row.get("request_ref") == state.request_ref for row in snapshots)):
                request = load_prepared_request(sink, state.request_ref)
                admission = {"expected_counters": request.expected_counters.to_payload(), "counters": request.counters.to_payload()}
                assert admission["expected_counters"] == admission["counters"] == state.counters.to_payload()
                snapshots.append({"leaf": state.leaf, "request_ref": state.request_ref, "admission": admission})
        durable.subscribe_commits(observe_freeze)
        try:
            with closing(connect_local(config=config)) as provider:
                configure(provider)
                wrapped = ObservedProvider(provider, False)
                result = run(durable, sink, wrapped)
                count = len(invocations)
                again = run(durable, sink, wrapped)
                assert again == result and len(invocations) == count
            counters = sink.counters().to_payload()
        finally:
            durable.close()
    records = [json.loads(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    generations = sum(row.get("type") == "request" for row in records)
    row = {"ok": result.completed and result.summary == "RETRY-RESTORED-731" and generations == 2
                  and len(invocations) == 2 and len(snapshots) == 2,
           "fault_injection": "Replace first genuine model response with invalid JSON after generation",
           "generations": generations, "seed": seed, "model": model, "result": result.to_payload(),
           "repeat_restore_stable": again == result, "invocations": invocations,
           "boundaries": snapshots, "final_counters": counters}
    (directory / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--seed", type=int, default=74)
    args = parser.parse_args()
    row = probe(args.output, base_url=args.base_url, model=args.model, seed=args.seed)
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0 if row["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
