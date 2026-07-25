from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey import provider_profiles


class ProviderProfileTests(unittest.TestCase):
    def test_bundled_profiles_are_versioned_and_complete(self) -> None:
        profiles = provider_profiles.load_profiles()

        self.assertEqual(set(profiles), {"deepseek", "qwen", "mimo", "stepfun", "glm"})
        for profile in profiles.values():
            self.assertGreaterEqual(profile.version, 1)
            self.assertTrue(profile.hosts)
            self.assertTrue(profile.selectors("message_box"))
            self.assertTrue(profile.selectors("send_button"))
            self.assertTrue(profile.selectors("response"))

        glm = profiles["glm"]
        self.assertIn("not(.empty)", glm.selector("send_button"))
        self.assertIn(
            ":not(.text-advance-thinking-content)",
            glm.selector("response"),
        )

    def test_profile_loader_rejects_unknown_schema(self) -> None:
        payload = {"schema_version": 999, "profiles": {}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profiles.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                provider_profiles.load_profiles(path)


if __name__ == "__main__":
    unittest.main()
