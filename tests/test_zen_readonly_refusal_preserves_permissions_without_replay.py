"""An observed FreeTier refusal cannot expand a readonly task's tool authority."""
import io
import json
import urllib.error

from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.zen.connection import ZenProvider


def test_observed_403_never_replays_or_changes_readonly_authority(tmp_path, monkeypatch):
    (tmp_path / "probe.txt").write_text("original", encoding="utf8")
    requests = []

    def refused(request, timeout):
        requests.append(json.loads(request.data))
        body = b'{"type":"error","error":{"type":"FreeTierError","message":"OpenCode\'s free tier can only be used from within OpenCode"}}'
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO(body))

    monkeypatch.setattr(api_transport, "open_request", refused)
    policy = TaskPolicy(frozenset({"control", "project.read"}))
    session = TaskSession(policy=policy, task_kind="planning_readonly", project=str(tmp_path), max_turns=3)
    provider = ZenProvider(ApiProvider("http://fixture.test/v1", "fixture", api_protocol="openai-responses", native_tools=True))
    result = run_task_kernel(session, request=KernelRunRequest(
        transport=KernelTransportDeps(provider=provider, user_task="Read probe.txt; do not modify files"),
        execution=KernelExecutionDeps(project_path=tmp_path)))
    assert result.stop_reason == "provider_failure" and not result.completed
    assert "403" in result.summary and "FreeTierError" in result.summary
    assert len(requests) == 1
    assert {tool["name"] for tool in requests[0]["tools"]} == {"done", "references", "search", "ls", "read", "shell"}
    shell = next(t for t in requests[0]["tools"] if t["name"] == "shell")
    assert "unavailable" in shell["description"].lower()
    assert session.policy == policy
    assert (tmp_path / "probe.txt").read_text(encoding="utf8") == "original"
