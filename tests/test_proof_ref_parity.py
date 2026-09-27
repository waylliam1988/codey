"""Parity guard for research_proof ref validation (Item 3).

Both Ghost completion (``codey.ghost.work_queue._research_proof_ref``) and
Research product (``codey.research.proof_quality._proof_ref_or_empty``) must
accept exactly ``research_proof:<16 lowercase hex>``. After the fix both
delegate to ``codey.utils.refs.research_proof_ref``; this battery locks the
boundary through both call paths so a future one-sided edit is caught.
"""
from __future__ import annotations

import unittest

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


class ProofRefParityTests(unittest.TestCase):
    def test_shared_helper_accepts_only_canonical_shape(self) -> None:
        from codey.utils.refs import research_proof_ref

        for ref in VALID:
            with self.subTest(ref=ref):
                self.assertEqual(research_proof_ref(ref), ref.strip())
        for ref in INVALID:
            with self.subTest(ref=ref):
                # trailing-space case normalizes via strip then validates
                if isinstance(ref, str) and ref == "research_proof:" + "a" * 16 + " ":
                    self.assertEqual(research_proof_ref(ref), ref.strip())
                else:
                    self.assertEqual(research_proof_ref(ref), "")

    def test_ghost_and_research_paths_agree(self) -> None:
        from codey.ghost.work_queue import _research_proof_ref as ghost_ref
        from codey.research.proof_quality import _proof_ref_or_empty as research_ref
        from codey.utils.refs import research_proof_ref as shared

        cases = list(VALID) + [c for c in INVALID if isinstance(c, str)]
        for ref in cases:
            with self.subTest(ref=ref):
                self.assertEqual(ghost_ref(ref), research_ref(ref))
                self.assertEqual(ghost_ref(ref), shared(ref))

    def test_whitespace_is_stripped_before_validation(self) -> None:
        from codey.utils.refs import research_proof_ref

        self.assertEqual(
            research_proof_ref("  research_proof:" + "b" * 16 + "\n"),
            "research_proof:" + "b" * 16,
        )

    def test_docstring_documents_str_coercion(self) -> None:
        # Doc wording must match the implementation: inputs are coerced with
        # str(value or "") (inherited behavior), not rejected by type check.
        from codey.utils.refs import research_proof_ref

        doc = (research_proof_ref.__doc__ or "").lower()
        self.assertTrue(
            "str(" in doc or "coerc" in doc,
            "docstring must document str-coercion semantics",
        )

    def test_non_string_scalars_fail_closed_through_all_paths(self) -> None:
        from codey.ghost.work_queue import _research_proof_ref as ghost_ref
        from codey.research.proof_quality import _proof_ref_or_empty as research_ref
        from codey.utils.refs import research_proof_ref as shared

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
                self.assertEqual(shared(value), "")
                self.assertEqual(ghost_ref(value), "")
                self.assertEqual(research_ref(value), "")


if __name__ == "__main__":
    unittest.main()
