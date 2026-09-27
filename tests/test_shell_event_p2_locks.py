"""P2 locks for event-shape shell command fields (red-first)."""
from __future__ import annotations


def test_event_accepts_legal_marker_substring_when_not_truncated() -> None:
    from codey.agents.shell_approval import shell_command_event_fields, shell_command_payload

    full = 'echo "[truncated; command_sha256="'
    fields = shell_command_payload(full)
    assert fields["command_truncated"] is False
    assert "[truncated; command_sha256=" in str(fields["command"])
    event = {
        "command": fields["command"],
        "command_sha256": fields["command_sha256"],
        "command_chars": fields["command_chars"],
        "command_truncated": fields["command_truncated"],
    }
    out = shell_command_event_fields(event)
    assert out["command"] == fields["command"]
    assert out["command_sha256"] == fields["command_sha256"]


def test_event_rejects_wrong_digest_when_not_truncated() -> None:
    import pytest

    from codey.agents.shell_approval import shell_command_event_fields

    with pytest.raises((ValueError, TypeError)):
        shell_command_event_fields(
            {
                "command": "echo hi",
                "command_sha256": "0" * 64,
                "command_chars": 7,
                "command_truncated": False,
            }
        )


def test_event_rejects_loose_marker_when_truncated() -> None:
    import pytest

    from codey.agents.shell_approval import shell_command_event_fields, shell_command_payload

    long_cmd = "python -c \"print('" + ("x" * 1600) + "')\""
    fields = shell_command_payload(long_cmd)
    assert fields["command_truncated"] is True
    digest = str(fields["command_sha256"])
    # Keep both substrings but break the exact trailing marker.
    loose = "prefix [truncated; command_sha256= middle " + digest + " suffix"
    assert "[truncated; command_sha256=" in loose
    assert digest in loose
    assert not loose.endswith(f"\n[truncated; command_sha256={digest}]")
    with pytest.raises((ValueError, TypeError)):
        shell_command_event_fields(
            {
                "command": loose,
                "command_sha256": digest,
                "command_chars": fields["command_chars"],
                "command_truncated": True,
            }
        )


def test_event_accepts_exact_trailing_marker_when_truncated() -> None:
    from codey.agents.shell_approval import shell_command_event_fields, shell_command_payload

    long_cmd = "python -c \"print('" + ("x" * 1600) + "')\""
    fields = shell_command_payload(long_cmd)
    event = {
        "command": fields["command"],
        "command_sha256": fields["command_sha256"],
        "command_chars": fields["command_chars"],
        "command_truncated": fields["command_truncated"],
    }
    out = shell_command_event_fields(event)
    assert out["command_truncated"] is True
    assert out["command_sha256"] == fields["command_sha256"]
