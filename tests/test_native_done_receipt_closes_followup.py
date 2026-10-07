from types import SimpleNamespace


def test_done_receipt_followup_is_closed_without_execution() -> None:
    from codey.operations import kernel_transport

    class Provider:
        def __init__(self):
            self.calls = []

        def send_tool_results(self, messages, tools):
            self.calls.append(messages)
            return SimpleNamespace(
                tool_calls=(SimpleNamespace(id="followup-1"),),
            )

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            return self.send_tool_results(results, [])

    provider = Provider()
    first = SimpleNamespace(tool_calls=(SimpleNamespace(id="done-1"),))
    followup = kernel_transport._take_answered_reply(
        provider, first, True, (), "done accepted"
    )

    assert followup is not None
    assert provider.calls[0][0].call_id == "done-1"
