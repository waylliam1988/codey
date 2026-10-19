"""The durable kernel imports and runs without Codey's host layers."""

import subprocess
import sys


def test_durable_kernel_does_not_import_the_application_or_runtime() -> None:
    script = """
import importlib.abc, sys
class HostLayersForbidden(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('codey.app', 'codey.agents', 'codey.providers', 'codey.runtime', 'codey.ghost', 'codey.research')):
            raise AssertionError('durable kernel imports host layer: ' + fullname)
sys.meta_path.insert(0, HostLayersForbidden())
from codey.durable import DurableSession, MemoryDurableStore
session = DurableSession(MemoryDurableStore())
session.admit_input(conversation_id='c', request_id='r', task_id='t', text='input', provider_snapshot={})
assert session.context_snapshot('c', token_budget=100).messages[0]['content'] == 'input'
session.close()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
