from __future__ import annotations

import unittest

from codey.agents.state import LoopStagnation, SeenInfoLRU, seen_info_key


class SeenInfoTests(unittest.TestCase):
    def test_key_hashes_long_model_text(self) -> None:
        big = "x" * 100_000
        key = seen_info_key("read", "a.py", big)
        self.assertEqual(len(key[2]), 24)
        self.assertNotIn(big, key)
        self.assertEqual(key, seen_info_key("read", "a.py", big))
        self.assertNotEqual(key, seen_info_key("read", "a.py", big + "y"))

    def test_lru_evicts_oldest_and_dedupes(self) -> None:
        seen = SeenInfoLRU(max_items=3)
        keys = [seen_info_key("read", f"{i}.py", f"body-{i}") for i in range(4)]
        for key in keys[:3]:
            self.assertTrue(seen.add(key))
        self.assertEqual(len(seen), 3)
        self.assertFalse(seen.add(keys[0]))
        self.assertTrue(seen.add(keys[3]))
        self.assertEqual(len(seen), 3)
        self.assertNotIn(keys[0], seen)
        self.assertIn(keys[3], seen)

    def test_stagnation_defaults_to_bounded_lru(self) -> None:
        stag = LoopStagnation()
        self.assertIsInstance(stag.seen_info, SeenInfoLRU)
        self.assertEqual(len(stag.seen_info), 0)


if __name__ == "__main__":
    unittest.main()
