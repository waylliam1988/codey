"""Scripted text transport for real workspace kernel tests."""
import atexit
import hashlib
import shutil
import tempfile
from pathlib import Path

from codey.providers.token_accounting import ContextBudget


class TextConversation:
    native_tools = False
    name = "Local"
    context_budget = ContextBudget(32768, 8192, 0, 12000)

    def bind_usage(self, connection_id, sink):
        pass

    def close(self):
        pass

    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []
        self.resets = 0

    def new_chat(self):
        self.resets += 1

    def send(self, prompt):
        self.requests.append(prompt)
        return next(self.replies)


def admit_entry_frame(frame, state):
    """Enter the shared mode kernel after its formal durable admission step."""
    from codey.operations.task_phases.lifecycle import _ensure_durable_task

    if _ensure_durable_task(state, frame.request):
        checkpoint = state.durable.task(frame.run_id)
        state.durable.start_task(frame.run_id, state=dict(checkpoint.state))


def isolated_short_state_home(request, source_path: Path) -> Path:
    """Use a short Windows state root and remove it after the test."""
    digest = hashlib.sha256(str(source_path).encode()).hexdigest()[:12]
    root = Path(tempfile.gettempdir()) / f"codey-{digest}"
    root.mkdir(parents=True, exist_ok=False)
    request.addfinalizer(lambda: shutil.rmtree(root, ignore_errors=True))
    return root


def short_test_state_home() -> Path:
    """Allocate a unique, short state root for Windows snapshot-path tests."""
    root = Path(tempfile.mkdtemp(prefix="cs-", dir=tempfile.gettempdir()))
    atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
    return root
