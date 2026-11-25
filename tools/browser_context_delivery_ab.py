"""Ablate observed context and recovery presentation over the production browser chain."""
from __future__ import annotations

import argparse
import json
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.automation.browser_observation import render_controls
from codey.automation.browser_session import MAX_SNAPSHOT_ELEMENTS, MAX_SNAPSHOT_TEXT, BrowserSession, BrowserToolOutput
from codey.operations.browser.browser_followups import pending_click
from codey.operations.browser.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_protocol import TurnSnapshot
from codey.operations.task_session import TaskSession
from codey.types.json_value import JsonObject, decode_json_object
from tools.browser_leaf_exposure_ab import Case, run_arm
from tools.browser_production_live_gate import select_target
from tools.completion_explanation_ab import write_json

HTML = '''<section aria-label="Order A-173"><h2>Order A-173</h2>
<button onclick="window.clicks=(window.clicks||0)+1;document.querySelector('article').textContent=
'Order A-173: receipt TARGET_173, delivery 18:20.'">View details</button></section>
<section aria-label="Order A-174"><h2>Order A-174</h2>
<button onclick="window.clicks=(window.clicks||0)+1;document.querySelector('article').textContent=
'Order A-174: receipt OTHER_174, delivery 09:10.'">View details</button></section>
<article>No query result yet.</article>'''
CASE = Case('请查询订单 A-173 的详情，报告其回执号和送达时间，读取操作后的正文并附来源。',
            HTML, ('TARGET_173', '18:20'), ('browser_click', 'browser_read'))


class Options(argparse.Namespace):
    run_dir: Path
    timeout: float


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args(namespace=Options())
    args.run_dir.mkdir(parents=True, exist_ok=False)
    target = select_target(args.timeout, 'http://127.0.0.1:5001/v1')
    original_snapshot = BrowserSession.snapshot
    original_capture = BrowserSession.capture_query_result
    results: list[JsonObject] = []
    for grouped in (False, True):
        for recovery in (False, True):
            for fault in (False, True):
                injected = False

                def snapshot(browser: BrowserSession, *, max_elements: int = MAX_SNAPSHOT_ELEMENTS,
                             max_text: int = MAX_SNAPSHOT_TEXT, file_inputs_only: bool = False,
                             _grouped: bool = grouped) -> BrowserToolOutput:
                    output = original_snapshot(browser, max_elements=max_elements, max_text=max_text,
                                               file_inputs_only=file_inputs_only)
                    if _grouped or file_inputs_only:
                        return output
                    value = dict(output.observation)
                    raw = value.get('elements')
                    rows = [decode_json_object(row) for row in raw] if isinstance(raw, list) else []
                    for row in rows:
                        row.pop('context_id', None)
                    value.pop('contexts', None)
                    value['elements'] = list(rows)
                    text = output.model_text.split('Interactive elements:\n')[0] + 'Interactive elements:\n'
                    return replace(output, observation=value, model_text=text + render_controls(rows, []))

                def capture(browser: BrowserSession, baseline: JsonObject, *, pending: str,
                            _fault: bool = fault) -> dict[str, object]:
                    nonlocal injected
                    if _fault and not injected:
                        injected = True
                        raise RuntimeError('Injected first post-click read failure; click is already delivered.')
                    return original_capture(browser, baseline, pending=pending)

                def handoff(session: TaskSession, frozen: TurnSnapshot, context: CompletionContext,
                            generation: int | None = None, _recovery: bool = recovery) -> tuple[TurnSnapshot, str]:
                    if not _recovery and pending_click(session._memory_results) is not None:
                        return frozen, 'The query read is unconfirmed. Reuse the delivered click.'
                    return browser_task_handoff(session, frozen, context, generation)

                name = f'context-{int(grouped)}-recovery-{int(recovery)}-fault-{int(fault)}'
                with ExitStack() as stack:
                    stack.enter_context(patch.object(BrowserSession, 'snapshot', snapshot))
                    stack.enter_context(patch.object(BrowserSession, 'capture_query_result', capture))
                    stack.enter_context(patch('codey.operations.browser.browser_task_handoff.browser_task_handoff', handoff))
                    row = run_arm(args.run_dir / name, CASE, 'A', target)
                row.update(context=grouped, recovery=recovery, injected=injected, fault=fault)
                results.append(row)
                status: JsonObject = {'case': name, 'passed': row['passed'], 'injected': injected}
                print(json.dumps(status), flush=True)
    write_json(args.run_dir / 'summary.json', results)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
