"""Parity guard for generated-ref validation.

Both Ghost completion (``codey.ghost.work_queue._research_proof_ref``) and
Research product (``codey.research.proof_quality._proof_ref_or_empty``) must
accept exactly ``research_proof:<16 lowercase hex>``. Both delegate to the
generic ``codey.utils.refs.generated_ref`` with their own prefix; this
battery locks the boundary through both call paths so a future one-sided
edit is caught. The generic helper itself is prefix-parameterized: wrong
prefix, uppercase hex, and bad lengths all fail closed.
"""
from __future__ import annotations

import unittest

PREFIX = "research_proof"
VALID = ("research_proof:" + "a" * 16, "research_proof:" + "0123456789abcdef")
INVALID = (
    "",
    "research_proof:",
    "research_proof:ABCDEF1234567890",  # uppercase rejected (fail-closed)
    "research_proof:abc123",  # too short
    "research_proof:" + "a" * 15,
    "research_proof:" + "a" * 17,
    "research_proof:" + "g" * 16,  # non-hex
    "research_proof: " + "a" * 16,  # inner space
    "research_record:" + "a" * 16,  # wrong prefix
    "research_proof:" + "a" * 16 + " ",
    None,
    123,
)


def _shared(value: object) -> str:
    from codey.utils.refs import generated_ref

    return generated_ref(value, PREFIX)


class ProofRefParityTests(unittest.TestCase):
    def test_shared_helper_accepts_only_canonical_shape(self) -> None:
        for ref in VALID:
            with self.subTest(ref=ref):
                self.assertEqual(_shared(ref), ref.strip())
        for ref in INVALID:
            with self.subTest(ref=ref):
                # trailing-space case normalizes via strip then validates
                if isinstance(ref, str) and ref == "research_proof:" + "a" * 16 + " ":
                    self.assertEqual(_shared(ref), ref.strip())
                else:
                    self.assertEqual(_shared(ref), "")

    def test_ghost_and_research_paths_agree(self) -> None:
        from codey.ghost.work_queue import _research_proof_ref as ghost_ref
        from codey.research.proof_quality import _proof_ref_or_empty as research_ref

        cases = list(VALID) + [c for c in INVALID if isinstance(c, str)]
        for ref in cases:
            with self.subTest(ref=ref):
                self.assertEqual(ghost_ref(ref), research_ref(ref))
                self.assertEqual(ghost_ref(ref), _shared(ref))

    def test_whitespace_is_stripped_before_validation(self) -> None:
        self.assertEqual(
            _shared("  research_proof:" + "b" * 16 + "\n"),
            "research_proof:" + "b" * 16,
        )

    def test_docstring_documents_str_coercion(self) -> None:
        # Doc wording must match the implementation: inputs are coerced with
        # str(value or "") (inherited behavior), not rejected by type check.
        from codey.utils.refs import generated_ref

        doc = (generated_ref.__doc__ or "").lower()
        self.assertTrue(
            "str(" in doc or "coerc" in doc,
            "docstring must document str-coercion semantics",
        )

    def test_generic_helper_is_prefix_parameterized(self) -> None:
        from codey.utils.refs import generated_ref

        self.assertEqual(generated_ref("artifact:" + "c" * 16, "artifact"), "artifact:" + "c" * 16)
        self.assertEqual(generated_ref("research_proof:" + "a" * 16, "artifact"), "")
        self.assertEqual(generated_ref("research_proof:" + "A" * 16, PREFIX), "")
        self.assertEqual(generated_ref("research_proof:" + "a" * 15, PREFIX), "")
        # No domain prefix constant lives in the generic layer.
        import codey.utils.refs as refs_mod

        self.assertNotIn("research_proof", refs_mod.generated_ref.__code__.co_consts)

    def test_non_string_scalars_fail_closed_through_all_paths(self) -> None:
        from codey.ghost.work_queue import _research_proof_ref as ghost_ref
        from codey.research.proof_quality import _proof_ref_or_empty as research_ref

        cases = [
            None,
            123,
            0,
            0.0,
            True,
            False,
            b"research_proof:" + b"a" * 16,
            ["research_proof:" + "a" * 16],
            {"ref": "research_proof:" + "a" * 16},
            ("research_proof:" + "a" * 16,),
            {"research_proof:" + "a" * 16},
        ]
        for value in cases:
            with self.subTest(value=repr(value)):
                self.assertEqual(_shared(value), "")
                self.assertEqual(ghost_ref(value), "")
                self.assertEqual(research_ref(value), "")


if __name__ == "__main__":
    unittest.main()
