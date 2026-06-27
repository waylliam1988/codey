from __future__ import annotations

import unittest

from codey.protocols import Action, XmlToolCodec


class XmlToolCodecTests(unittest.TestCase):
    def test_parse_xml_tool_plan(self) -> None:
        codec = XmlToolCodec()
        actions, control = codec.parse(
            """<codey>
  <tool name="search"><path>.</path><query>LOGIN_BUG</query></tool>
  <control type="continue">need results</control>
</codey>"""
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "search")
        self.assertEqual(actions[0].path, ".")
        self.assertEqual(actions[0].body, "LOGIN_BUG")
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")

    def test_old_marker_protocol_is_not_parsed(self) -> None:
        codec = XmlToolCodec()
        actions, control = codec.parse(
            """```text
# === codey: read path=app.py ===
```"""
        )

        self.assertEqual(actions, [])
        self.assertIsNone(control)

    def test_format_results_and_repair_prompt_remind_xml_only(self) -> None:
        codec = XmlToolCodec()
        prompt = codec.format_results([(Action("read", "app.py", ""), "content")])

        self.assertIn("<codey>...</codey>", prompt)
        self.assertIn("[read app.py]\ncontent", prompt)
        self.assertIn("<codey>...</codey>", codec.repair_prompt())


if __name__ == "__main__":
    unittest.main()
