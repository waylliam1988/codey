"""Experiment controls around the production headless entry, not a second agent loop."""

from __future__ import annotations

import argparse
import os
import threading
from pathlib import Path

from codey.app import headless_runner
from codey.providers.registry import connect_provider


class ControlledContext(headless_runner.HeadlessAppContext):
    def __init__(self, state_home, *args, **kwargs):
        from codey.providers.model_preferences import ModelPreferences

        preferences = ModelPreferences(state_home)
        snapshot = preferences.snapshot()
        sources = {key: {"enabled": False, "models": []} for key in snapshot["sources"]}
        sources["local"] = {"enabled": True, "models": [os.environ["LOCAL_OPENAI_MODEL"]]}
        if snapshot["sources"] != sources:
            preferences.save(sources, base_revision=snapshot["revision"])
        super().__init__(state_home, *args, **kwargs)
        self._observer_closed = threading.Event()
        self._observer_thread = threading.Thread(target=self._observe_stop, daemon=True)
        self._observer_thread.start()

    def _observe_stop(self):
        control = Path(os.environ["CODEY_AB_TRACE"]) / "stop"
        while not self._observer_closed.wait(.02):
            if control.exists():
                self.request_stop()  # Same production stop action used by the UI.
                return

    def close(self):
        self._observer_closed.set()
        self._observer_thread.join(1)
        return super().close()


def sampling_provider(provider_id, **kwargs):
    if provider_id != "local":
        raise RuntimeError("This experiment permits only Local providers")
    provider = connect_provider(provider_id, **kwargs)
    configure = provider.configure_request
    provider.temperature = float(os.environ["CODEY_AB_TEMPERATURE"])

    def before_count(payload):
        configure(payload)
        payload["seed"] = int(os.environ["CODEY_AB_SEED"])
        payload["temperature"] = float(os.environ["CODEY_AB_TEMPERATURE"])

    provider.configure_request = before_count
    return provider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--state-home", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--continue", dest="continue_task", action="store_true")
    parser.add_argument("--max-turns", type=int, default=1000)
    parser.add_argument("task")
    args = parser.parse_args()
    headless_runner.HeadlessAppContext = ControlledContext

    def emit(payload):
        headless_runner.emit_jsonl(payload)
        import sys
        sys.stdout.flush()

    request = headless_runner.HeadlessRequest(project=args.project, task=args.task,
        provider_id="local", state_home=args.state_home, session_id=args.session_id,
        continue_task=args.continue_task, max_turns=args.max_turns)
    return headless_runner.run_headless(request, emit_jsonl=emit, connect_provider=sampling_provider,
                                       connect_reviewer=sampling_provider).exit_code


if __name__ == "__main__":
    raise SystemExit(main())
