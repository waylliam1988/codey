"""The shared test helper itself: hermetic state, never the real home."""

from __future__ import annotations

import unittest

from codey.agents.handoff import ConversationSnapshot
from codey.storage.local_store import DEFAULT_STATE_HOME, session_key
from tests.app_state import make_app_state


class AppStateHelperTests(unittest.TestCase):
    def test_persistence_lands_under_temp_dir_not_home(self) -> None:
        session_id = "hermetic-probe-session"
        state = make_app_state(self)
        conversation = state.conversation_for(session_id)
        conversation.begin_window("deepseek", "chat")
        conversation.update_snapshot(
            ConversationSnapshot(mode="chat", goal="probe", provider_id="deepseek")
        )

        home_path = (
            DEFAULT_STATE_HOME / "conversations" / f"{session_key(session_id)}.json"
        )
        self.assertFalse(home_path.exists())
        self.assertTrue(state.conversation_registry.store.path_for(session_id).exists())


if __name__ == "__main__":
    unittest.main()
