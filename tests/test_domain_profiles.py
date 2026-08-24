from __future__ import annotations

import unittest

from codey.research.domain_profiles import (
    BUILTIN_PROFILES,
    BUILTIN_PROFILE_IDS,
    EvidenceProfile,
    GENERAL_PROFILE,
    MAX_MERGE_PROFILES,
    merge_profiles,
    resolve_profile,
)


class BuiltinProfileTests(unittest.TestCase):
    def test_exactly_six_atomic_builtin_profiles_exist(self) -> None:
        self.assertEqual(tuple(sorted(BUILTIN_PROFILES)), BUILTIN_PROFILE_IDS)
        self.assertEqual(
            set(BUILTIN_PROFILES),
            {
                "general",
                "science",
                "finance",
                "legal",
                "market",
                "software_research",
            },
        )

    def test_no_combination_or_inherited_profiles_exist(self) -> None:
        for profile_id in BUILTIN_PROFILES:
            with self.subTest(profile_id=profile_id):
                self.assertNotIn("+", profile_id)
                self.assertNotIn("_legal", profile_id)
                self.assertNotIn("_finance", profile_id)

    def test_general_profile_is_the_neutral_baseline(self) -> None:
        payload = GENERAL_PROFILE.to_payload()
        self.assertEqual(payload["freshness_expectation"], "low")
        self.assertEqual(payload["source_quality_threshold"], "any")
        self.assertEqual(payload["primary_source_preference"], "allowed")
        self.assertEqual(payload["preferred_connector_kinds"], [])

    def test_domain_strictness_directions_are_locked(self) -> None:
        finance = BUILTIN_PROFILES["finance"].to_payload()
        legal = BUILTIN_PROFILES["legal"].to_payload()
        science = BUILTIN_PROFILES["science"].to_payload()
        market = BUILTIN_PROFILES["market"].to_payload()
        software = BUILTIN_PROFILES["software_research"].to_payload()

        # finance: fresh data + local analysis for data claims
        self.assertEqual(finance["freshness_expectation"], "high")
        self.assertEqual(finance["analysis_for_data_claims"], "required")
        self.assertIn("filing", finance["preferred_source_kinds"])

        # legal: primary sources required
        self.assertEqual(legal["primary_source_preference"], "required")
        self.assertEqual(legal["source_quality_threshold"], "primary")

        # science: counterevidence required
        self.assertEqual(science["counterevidence_requirement"], "required")

        # market: freshest expectations
        self.assertEqual(market["freshness_expectation"], "critical")

        # software research: repository/release/issue preferred
        self.assertIn("repository", software["preferred_source_kinds"])
        self.assertIn("release", software["preferred_source_kinds"])
        self.assertIn("issue", software["preferred_source_kinds"])


class ResolveProfileTests(unittest.TestCase):
    def test_empty_labels_resolve_to_general_without_warning(self) -> None:
        resolved = resolve_profile([])

        self.assertEqual(resolved.profile_id, "general")
        self.assertEqual(resolved.warnings, ())

    def test_unknown_label_falls_back_with_warning_not_guess(self) -> None:
        resolved = resolve_profile(["crypto_stuff"])

        self.assertEqual(resolved.profile_id, "general")
        self.assertIn("unknown_profile_label", resolved.warnings)

    def test_unknown_label_along_known_label_keeps_known_and_warns(self) -> None:
        resolved = resolve_profile(["finance", "biotech_stuff"])

        self.assertEqual(resolved.profile_id, "finance")
        self.assertIn("unknown_profile_label", resolved.warnings)

    def test_single_label_returns_that_profile(self) -> None:
        resolved = resolve_profile(["science"])

        self.assertEqual(resolved.profile_id, "science")
        self.assertEqual(resolved.warnings, ())

    def test_duplicate_labels_dedupe(self) -> None:
        first = resolve_profile(["finance", "finance", "FINANCE"])
        second = resolve_profile(["finance"])

        self.assertEqual(first.profile_id, "finance")
        self.assertEqual(first, second)

    def test_merge_cap_is_bounded_and_warns(self) -> None:
        labels = ["finance", "legal", "science", "market", "software_research"]
        resolved = resolve_profile(labels)

        self.assertLessEqual(len(resolved.profile_id.split("+")), MAX_MERGE_PROFILES)
        self.assertIn("profile_merge_truncated", resolved.warnings)


class MergeProfileTests(unittest.TestCase):
    def test_cross_domain_composition_takes_stricter_per_dimension(self) -> None:
        merged = merge_profiles(
            BUILTIN_PROFILES["finance"],
            BUILTIN_PROFILES["legal"],
        )

        payload = merged.to_payload()
        self.assertEqual(merged.profile_id, "finance+legal")
        # freshness: max(high, high)
        self.assertEqual(payload["freshness_expectation"], "high")
        # primary source: max(preferred, required)
        self.assertEqual(payload["primary_source_preference"], "required")
        # quality floor: max(secondary, primary)
        self.assertEqual(payload["source_quality_threshold"], "primary")
        # analysis: max(required, optional)
        self.assertEqual(payload["analysis_for_data_claims"], "required")
        # counterevidence: max(preferred, preferred)
        self.assertEqual(payload["counterevidence_requirement"], "preferred")

    def test_tuple_dimensions_union_without_duplication(self) -> None:
        merged = merge_profiles(
            BUILTIN_PROFILES["finance"],
            BUILTIN_PROFILES["science"],
        )

        self.assertEqual(
            merged.preferred_source_kinds,
            ("dataset", "filing", "official", "peer_reviewed", "preprint"),
        )
        self.assertEqual(
            merged.disfavored_source_kinds,
            ("aggregator", "forum", "social"),
        )
        self.assertEqual(merged.preferred_connector_kinds, ("data", "paper"))

    def test_merge_is_order_insensitive_for_values(self) -> None:
        one = merge_profiles(BUILTIN_PROFILES["finance"], BUILTIN_PROFILES["legal"]).to_payload()
        two = merge_profiles(BUILTIN_PROFILES["legal"], BUILTIN_PROFILES["finance"]).to_payload()

        # The merged id records composition order; every expectation value
        # must be identical regardless of order.
        one.pop("profile_id")
        two.pop("profile_id")
        self.assertEqual(one, two)

    def test_merged_payload_keeps_composition_marker(self) -> None:
        merged = merge_profiles(
            BUILTIN_PROFILES["finance"],
            BUILTIN_PROFILES["legal"],
        )

        payload_id = merged.to_payload()["profile_id"]

        # "+" must survive payload sanitization: "finance_legal" would look
        # exactly like a builtin combination profile, which never exists.
        self.assertEqual(payload_id, "finance+legal")
        self.assertNotIn(payload_id, BUILTIN_PROFILES)

    def test_nested_merge_flattens_segments_and_never_mints_combo_names(self) -> None:
        composed = merge_profiles(
            BUILTIN_PROFILES["finance"],
            BUILTIN_PROFILES["legal"],
        )

        nested = merge_profiles(composed, BUILTIN_PROFILES["science"])
        flat = resolve_profile(["finance", "legal", "science"])

        self.assertEqual(nested.profile_id, "finance+legal+science")
        self.assertEqual(nested.to_payload(), flat.to_payload())
        for forbidden in ("finance_legal", "finance_legal_science", "legal_science"):
            self.assertNotIn(forbidden, nested.profile_id)
        self.assertEqual(
            merge_profiles(nested, BUILTIN_PROFILES["finance"]).profile_id,
            "finance+legal+science",
        )

    def test_merge_never_creates_new_atomic_profile_entries(self) -> None:
        before = dict(BUILTIN_PROFILES)
        merged = merge_profiles(
            BUILTIN_PROFILES["finance"],
            BUILTIN_PROFILES["legal"],
            BUILTIN_PROFILES["science"],
        )

        self.assertEqual(BUILTIN_PROFILES, before)
        self.assertNotIn(merged.profile_id, BUILTIN_PROFILES)
        self.assertEqual(merged.profile_id, "finance+legal+science")

    def test_merge_caps_unique_profiles_and_warns(self) -> None:
        profiles = [
            BUILTIN_PROFILES[name]
            for name in ("finance", "legal", "science", "market", "software_research")
        ]

        merged = merge_profiles(*profiles)

        self.assertEqual(len(merged.profile_id.split("+")), MAX_MERGE_PROFILES)
        self.assertIn("profile_merge_truncated", merged.warnings)

    def test_nested_merge_caps_flattened_atomic_segments_before_values(self) -> None:
        composed = merge_profiles(
            BUILTIN_PROFILES["finance"],
            BUILTIN_PROFILES["legal"],
            BUILTIN_PROFILES["market"],
            BUILTIN_PROFILES["science"],
        )

        merged = merge_profiles(composed, BUILTIN_PROFILES["software_research"])

        self.assertEqual(merged.profile_id, "finance+legal+market+science")
        self.assertEqual(len(merged.profile_id.split("+")), MAX_MERGE_PROFILES)
        self.assertIn("profile_merge_truncated", merged.warnings)
        self.assertNotIn("software_research", merged.profile_id)
        self.assertNotIn("repository", merged.preferred_source_kinds)
        self.assertNotIn("release", merged.preferred_source_kinds)
        self.assertNotIn("issue", merged.preferred_source_kinds)

    def test_merge_ignores_non_profiles_and_empty_input(self) -> None:
        self.assertEqual(merge_profiles().profile_id, "general")
        junk = merge_profiles("finance", None)  # type: ignore[arg-type]
        self.assertEqual(junk.profile_id, "general")


class PayloadTests(unittest.TestCase):
    def test_payload_normalizes_junk_values_instead_of_leaking_them(self) -> None:
        profile = EvidenceProfile(
            profile_id="weird id!!",
            freshness_expectation="whenever",
            source_quality_threshold="shiny",
            preferred_source_kinds=("peer_reviewed", "", "peer_reviewed"),
            warnings=("a" * 300,),
        )

        payload = profile.to_payload()

        self.assertEqual(payload["profile_id"], "weird_id")
        self.assertEqual(payload["freshness_expectation"], "low")
        self.assertEqual(payload["source_quality_threshold"], "any")
        self.assertEqual(payload["preferred_source_kinds"], ["peer_reviewed"])
        self.assertEqual(payload["warnings"], ["a" * 117 + "..."])
        self.assertLessEqual(len(payload["warnings"][0]), 120)

    def test_payload_is_json_serializable_and_small(self) -> None:
        import json

        for profile_id, profile in BUILTIN_PROFILES.items():
            with self.subTest(profile_id=profile_id):
                data = json.dumps(profile.to_payload())
                self.assertLess(len(data), 600)


if __name__ == "__main__":
    unittest.main()
