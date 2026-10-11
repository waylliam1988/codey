"""Scripted text transport for real workspace kernel tests."""
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
