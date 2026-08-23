from __future__ import annotations

import unittest

from codey.research.brief_projection import (
    MAX_HANDOFF_CHARS,
    ClaimSummary,
    build_impact_contract,
    constraints_from_claims,
    project_research_brief,
    render_handoff,
)
from codey.research.evidence_runtime import snapshot_from_research_record


def _record_payload() -> dict[str, object]:
    return {
        "record_id": "research_record:" + "a" * 16,
        "record_digest": "sha256:" + "b" * 64,
        "answer_status": "answered",
        "sources": [
            {"source_id": "source:" + "1" * 16, "host": "sec.gov"},
            {"source_id": "source:" + "2" * 16, "host": "arxiv.org"},
        ],
        "evidence": [
            {"evidence_id": "evidence:" + "3" * 16, "source_id": "source:" + "1" * 16},
            {"evidence_id": "evidence:" + "4" * 16, "source_id": "source:" + "2" * 16},
        ],
        "claims": [
            {
                "claim_id": "claim:" + "5" * 16,
                "claim_text": "The API flow is documented.",
                "claim_section": "conclusion",
                "status": "evidence_backed",
                "evidence_refs": ["evidence:" + "3" * 16],
            },
            {
                "claim_id": "claim:" + "6" * 16,
                "claim_text": "Latency may improve next quarter.",
                "claim_section": "conclusion",
                "status": "unsupported",
                "evidence_refs": [],
            },
            {
                "claim_id": "claim:" + "7" * 16,
                "claim_text": "Assuming the rate limit stays at 60 rpm.",
                "claim_section": "counter",
                "status": "assumption",
                "assumption_refs": ["assumption:" + "8" * 16],
                "evidence_refs": [],
            },
        ],
        "assumptions": [
            {"assumption_id": "assumption:" + "8" * 16, "assumption_text": "rate limit stays"},
        ],
        "relations": [],
    }


class ProjectResearchBriefTests(unittest.TestCase):
    def test_returns_none_without_anchor_record(self) -> None:
        self.assertIsNone(project_research_brief(None))
        self.assertIsNone(project_research_brief({"record_id": "junk"}))

    def test_projection_is_refs_only_with_bounded_claim_texts(self) -> None:
        projection = project_research_brief(_record_payload())

        self.assertIsNotNone(projection)
        self.assertEqual(projection.record_ref, "research_record:" + "a" * 16)
        counts = projection.counts()
        self.assertEqual(counts["claims"], 3)
        self.assertEqual(counts["sources"], 2)
        self.assertEqual(counts["evidence"], 0)  # no snapshot: no evidence refs
        for claim in projection.claims:
            with self.subTest(claim=claim.claim_ref):
                self.assertLessEqual(len(claim.text), 260)
        payload = projection.to_payload()
        blob = str(payload)
        self.assertNotIn("<html", blob.lower())
        self.assertNotIn("transcript", blob)

    def test_unsupported_claim_presence_is_warned(self) -> None:
        projection = project_research_brief(_record_payload())

        self.assertIn("unsupported_claim_present", projection.warnings)

    def test_snapshot_path_carries_runtime_refs(self) -> None:
        record = _record_payload()
        review = {"proof_ref": "research_proof:" + "9" * 16}
        snapshot = snapshot_from_research_record(record, proof_review=review)

        projection = project_research_brief(record, snapshot=snapshot)

        self.assertIsNotNone(projection)
        self.assertEqual(projection.counts()["evidence"], 2)
        self.assertEqual(
            projection.proof_review_refs,
            ("research_proof:" + "9" * 16,),
        )
        self.assertNotIn("projection_without_runtime_snapshot", projection.warnings)


class ImpactContractTests(unittest.TestCase):
    def test_supported_claims_back_verified_constraints(self) -> None:
        projection = project_research_brief(_record_payload())
        supported = constraints_from_claims(projection.supported_claims())

        self.assertEqual(len(supported), 1)
        self.assertEqual(supported[0].support, "verified")
        self.assertEqual(supported[0].claim_refs, ("claim:" + "5" * 16,))

    def test_unsupported_claims_never_enter_constraints(self) -> None:
        claims = [
            ClaimSummary(claim_ref="claim:" + "6" * 16, text="Latency may improve.", status="unsupported"),
            ClaimSummary(claim_ref="claim:" + "7" * 16, text="Assume rate limit.", status="assumption"),
        ]

        contract = build_impact_contract(claims=claims)

        self.assertIsNotNone(contract)
        self.assertEqual(contract.implementation_constraints, ())
        joined = " ".join(contract.risk_notes)
        self.assertIn("[unsupported_claim]", joined)
        self.assertIn("[declared_assumption]", joined)

    def test_affected_files_validation_drops_escape_paths(self) -> None:
        contract = build_impact_contract(
            affected_files=[
                "src/api.py",
                "../escape.py",
                "/absolute/path.py",
                "C:\\windows\\path.py",
                "has..dots.py",
            ],
        )

        self.assertEqual(contract.affected_files, ("src/api.py",))

    def test_empty_contract_is_none(self) -> None:
        self.assertIsNone(build_impact_contract())

    def test_test_suggestions_and_files_stay_bounded_context(self) -> None:
        contract = build_impact_contract(
            claims=[ClaimSummary(claim_ref="claim:" + "5" * 16, text="Documented flow.", status="evidence_backed")],
            affected_files=["src/api.py"],
            test_suggestions=["pytest tests/test_api.py"],
            out_of_scope_items=["frontend rewrite"],
            decision_refs=["20250101T000000-decision"],
        )

        payload = contract.to_payload()
        self.assertEqual(payload["affected_files"], ["src/api.py"])
        self.assertEqual(payload["test_suggestions"], ["pytest tests/test_api.py"])
        self.assertEqual(payload["out_of_scope_items"], ["frontend rewrite"])
        self.assertEqual(len(payload["implementation_constraints"]), 1)


class RenderHandoffTests(unittest.TestCase):
    def test_render_is_short_structured_and_labeled(self) -> None:
        record = _record_payload()
        snapshot = snapshot_from_research_record(record)
        projection = project_research_brief(record, snapshot=snapshot)
        impact = build_impact_contract(
            claims=projection.claims,
            affected_files=["src/api.py"],
            test_suggestions=["pytest tests/test_api.py"],
        )

        rendered = render_handoff(projection, impact)

        self.assertLessEqual(len(rendered), MAX_HANDOFF_CHARS)
        self.assertIn("Concluded (verified support):", rendered)
        self.assertIn("Uncertain / assumptions:", rendered)
        self.assertIn("[unsupported]", rendered)
        self.assertIn("Implementation impact (research-derived, context only):", rendered)
        self.assertIn("not authorized by this handoff", rendered)

    def test_render_without_impact_stays_valid(self) -> None:
        projection = project_research_brief(_record_payload())

        rendered = render_handoff(projection, None)

        self.assertIn("record:", rendered)
        self.assertNotIn("Implementation impact", rendered)


if __name__ == "__main__":
    unittest.main()
