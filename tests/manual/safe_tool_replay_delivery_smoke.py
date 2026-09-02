"""Manual smoke test for safe tool replay result delivery receipts (0.5.5).

Verifies multi-safe-tool-per-turn crash-recovery and prompt delivery receipt flow:
1. --self-test: Injects crash between settled tool #1 and pending tool #2 in same turn,
   resumes via recover_effects_for_resume, delivers batch prompt, settles to provider ok.
2. --same-run-self-test: Executes successive safe tool turns, verifying continuous receipts.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

from codey.agents.loop import run as run_agent_loop
from codey.agents.request import AgentRequest
from codey.operations.recovery import recover_effects_for_resume
from codey.runtime.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    SETTLEMENT_STATUS_OK,
    new_effect_id,
)
from codey.runtime.effects import RuntimeOperationStore
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import RuntimeSessionLog
from codey.runtime.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryStore,
    compute_batch_digest,
    new_batch_id,
)


class ScriptedProvider:
    name = "ScriptedProvider"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.received_prompts: list[str] = []

    def send(self, prompt: str) -> str:
        self.received_prompts.append(prompt)
        if self.replies:
            return self.replies.pop(0)
        return '{"tool":"done","args":{"summary":"finished"}}'


def run_self_test() -> int:
    print("[SMOKE] Starting safe replay result delivery self-test...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        target = root / "module.py"
        target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        state_dir = root / "state"
        log = RuntimeSessionLog(state_dir)
        operations = RuntimeOperationStore(log)
        effects = RuntimeEffectStore(log)
        delivery = ToolResultDeliveryStore(log)

        session_id = "smoke-sess-1"
        run_id = "smoke-run-1"
        operations.start(
            session_id=session_id,
            run_id=run_id,
            project=str(root),
            provider_id="ScriptedProvider",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="project",
        )

        # 1. Simulate crashed turn 1:
        # Tool 0: read (settled)
        eff_read = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, run_id)
        effects.record_intent(
            session_id,
            run_id,
            RuntimeEffectIntent(
                effect_id=eff_read,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=session_id,
                run_id=run_id,
                phase="writer",
                turn=1,
                tool_index=0,
                tool_name="read",
                replay_class=ReplayClass.SAFE,
                replay_args={"path": "module.py"},
            ),
        )
        effects.record_settlement(
            session_id,
            run_id,
            RuntimeEffectSettlement(
                effect_id=eff_read,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=session_id,
                run_id=run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state="settled",
                replay_class=ReplayClass.SAFE,
            ),
        )

        # Tool 1: search (pending, crash before settlement)
        eff_search = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, run_id)
        effects.record_intent(
            session_id,
            run_id,
            RuntimeEffectIntent(
                effect_id=eff_search,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=session_id,
                run_id=run_id,
                phase="writer",
                turn=1,
                tool_index=1,
                tool_name="search",
                replay_class=ReplayClass.SAFE,
                replay_args={"path": ".", "query": "def add"},
            ),
        )

        # Delivery batch recorded before prompt was sent
        batch_id = new_batch_id(run_id, 1)
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=eff_read,
                replay_class="safe",
                is_denied=False,
            ),
            DeliveryBatchItem(
                tool_index=1,
                tool_name="search",
                ref=eff_search,
                replay_class="safe",
                is_denied=False,
            ),
        )
        delivery.record_batch_intent(
            session_id,
            run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=session_id,
                run_id=run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )

        # 2. Perform recovery
        class RecoveryDeps:
            runtime_effects = effects
            tool_result_delivery = delivery

        recovery = recover_effects_for_resume(
            RecoveryDeps(),
            session_id=session_id,
            run_id=run_id,
            project=str(root),
            task_kind="project",
        )
        assert recovery.ok, "Recovery failed"
        assert len(recovery.recovered_tool_outcomes) == 2, f"Expected 2 recovered outcomes, got {len(recovery.recovered_tool_outcomes)}"
        assert recovery.recovered_tool_result_batch_id == batch_id, "Batch ID mismatch"
        print("[SMOKE] Multi-tool batch successfully recovered in exact order.")

        # 3. Resume agent loop with recovered results
        provider = ScriptedProvider([
            '{"tool":"done","args":{"summary":"verified add function"}}',
        ])
        request = AgentRequest(
            provider=provider,
            project=root,
            task="inspect add function",
            session_id=session_id,
            run_id=run_id,
            permission_profile="coding_writer",
            runtime_effects=effects,
            tool_result_delivery=delivery,
            recovered_tool_outcomes=recovery.recovered_tool_outcomes,
            recovered_tool_result_batch_id=recovery.recovered_tool_result_batch_id,
        )
        result = run_agent_loop(request)
        assert result.stop_reason == "done", f"Expected stop_reason 'done', got {result.stop_reason!r}"
        assert len(provider.received_prompts) == 1, "Expected 1 prompt delivered on resume"

        # Verify delivered batch projection
        batches = delivery.load_batches(session_id, run_id)
        assert len(batches) == 1, f"Expected 1 batch, got {len(batches)}"
        assert batches[0].is_delivered, "Batch should be marked delivered after provider response"
        assert batches[0].is_recovered, "Batch should retain recovered status"
        print("[SMOKE] Delivered batch receipt verified.")

        # 4. Subsequent recovery is idempotent (no duplicate re-replay)
        recovery2 = recover_effects_for_resume(
            RecoveryDeps(),
            session_id=session_id,
            run_id=run_id,
            project=str(root),
            task_kind="project",
        )
        assert recovery2.ok, "Subsequent recovery failed"
        assert len(recovery2.recovered_tool_outcomes) == 0, "Expected 0 outcomes on subsequent recovery"
        print("[SMOKE] Subsequent recovery idempotency verified.")

    print("[SMOKE] All self-tests passed successfully!")
    return 0


def run_same_run_self_test() -> int:
    print("[SMOKE] Starting same-run multi-turn delivery receipt smoke...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        (root / "a.txt").write_text("file a", encoding="utf-8")
        (root / "b.txt").write_text("file b", encoding="utf-8")

        state_dir = root / "state"
        log = RuntimeSessionLog(state_dir)
        operations = RuntimeOperationStore(log)
        effects = RuntimeEffectStore(log)
        delivery = ToolResultDeliveryStore(log)

        session_id = "smoke-same-run-1"
        run_id = "smoke-same-run-1"
        operations.start(
            session_id=session_id,
            run_id=run_id,
            project=str(root),
            provider_id="ScriptedProvider",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="project",
        )

        # 2 turns of safe tool execution then done
        provider = ScriptedProvider([
            '{"tool":"read","args":{"path":"a.txt"}}',
            '{"tool":"read","args":{"path":"b.txt"}}',
            '{"tool":"done","args":{"summary":"read both files"}}',
        ])
        request = AgentRequest(
            provider=provider,
            project=root,
            task="read files a and b",
            session_id=session_id,
            run_id=run_id,
            permission_profile="coding_writer",
            runtime_effects=effects,
            tool_result_delivery=delivery,
        )
        result = run_agent_loop(request)
        assert result.stop_reason == "done", f"Expected done, got {result.stop_reason!r}"

        # Verify all batches were delivered
        batches = delivery.load_batches(session_id, run_id)
        assert len(batches) == 2, f"Expected 2 delivered batches, got {len(batches)}"
        for b in batches:
            assert b.is_delivered, f"Batch {b.intent.batch_id} should be delivered"
        print(f"[SMOKE] Verified {len(batches)} turns with full two-phase delivery receipts.")

    print("[SMOKE] Same-run self-test passed successfully!")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="0.5.5 Safe Tool Replay Result Delivery Smoke")
    parser.add_argument("--self-test", action="store_true", help="Run crash-recovery smoke test")
    parser.add_argument("--same-run-self-test", action="store_true", help="Run multi-turn same-run smoke test")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    if args.same_run_self_test:
        return run_same_run_self_test()

    # Default: run both
    ret = run_self_test()
    if ret != 0:
        return ret
    return run_same_run_self_test()


if __name__ == "__main__":
    sys.exit(main())
