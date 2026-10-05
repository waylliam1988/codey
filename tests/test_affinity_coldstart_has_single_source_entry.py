"""A fresh store consumes the source owner without a dormant forwarding API."""

from __future__ import annotations

import subprocess
import sys


def test_cold_store_has_one_source_entry_and_replays_real_specs(tmp_path):
    script = """
import sys
from types import SimpleNamespace
from codey.ghost.affinity import GhostAffinityStore

assert not hasattr(GhostAffinityStore, '_source_specs')
hebbian = SimpleNamespace(list_nodes=lambda **kwargs: [SimpleNamespace(
    id='h-audit', kind='research_interest', status='active', superseded_by='',
    scope='user', scope_ref='', conflict_key='topic', value_key='astronomy',
    confidence=0.8, weight=0.6, evidence_refs=('e-audit',),
)])
store = GhostAffinityStore(sys.argv[1])
result = store.sync_from_sources(hebbian_store=hebbian)
assert result.ok and result.nodes_changed > 0, result
nodes = store.list_nodes()
assert nodes and any('astronomy' in node.key for node in nodes), nodes
reopened = GhostAffinityStore(sys.argv[1])
assert [node.to_payload() for node in reopened.list_nodes()] == [node.to_payload() for node in nodes]
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
