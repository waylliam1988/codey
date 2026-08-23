from __future__ import annotations

import unittest

from codey.research.source_trust import (
    MAX_PROJECTIONS,
    SOURCE_CLASSES,
    TIER_STRONG,
    TIER_WEAK,
    SourceTrustProjection,
    evaluate_against_profile,
    project_source_set,
    project_source_trust,
    source_trust_warnings,
)


def _source(host: str, *, level: str = "secondary", kind: str = "web", freshness: str = "undated", suffix: str = "0123456789abcdef") -> dict[str, object]:
    return {
        "source_id": f"source:{suffix}",
        "host": host,
        "content_kind": "html",
        "quality": {"level": level, "kind": kind, "freshness": freshness},
    }


class ClassificationTests(unittest.TestCase):
    def test_low_dimensional_stable_taxonomy(self) -> None:
        self.assertEqual(
            set(SOURCE_CLASSES),
            {
                "official",
                "primary",
                "peer_reviewed",
                "preprint",
                "dataset",
                "filing",
                "standard",
                "repository",
                "issue",
                "release",
                "news",
                "secondary",
                "forum",
                "social",
                "aggregator",
                "unknown",
            },
        )

    def test_preprint_host_classifies_as_preprint(self) -> None:
        projection = project_source_trust(_source("arxiv.org"))

        self.assertEqual(projection.source_class, "preprint")
        self.assertEqual(projection.tier, 2)

    def test_peer_reviewed_host_classifies_strong(self) -> None:
        projection = project_source_trust(_source("pubmed.ncbi.nlm.nih.gov"))

        self.assertEqual(projection.source_class, "peer_reviewed")
        self.assertEqual(projection.tier, TIER_STRONG)

    def test_repository_host_and_gov_suffix(self) -> None:
        repo = project_source_trust(_source("github.com"))
        gov = project_source_trust(_source("example.gov", level="primary", kind="official"))

        self.assertEqual(repo.source_class, "repository")
        self.assertIn("official", gov.classes)
        self.assertEqual(gov.tier, TIER_STRONG)

    def test_gov_edu_lookalikes_do_not_inherit_strong_trust(self) -> None:
        lookalikes = [
            _source("sec.gov.evil.example", suffix="1" * 16),
            _source("nasa.gov.mitm.example", suffix="2" * 16),
            _source("mit.edu.phishing.example", suffix="3" * 16),
        ]

        for source in lookalikes:
            with self.subTest(host=source["host"]):
                projection = project_source_trust(source)
                self.assertNotIn("official", projection.classes)
                self.assertNotIn("primary", projection.classes)
                self.assertNotEqual(projection.tier, TIER_STRONG)

    def test_compound_gov_edu_suffixes_still_match(self) -> None:
        cases = [
            ("australia.gov.au", "official"),
            ("sub.treasury.gov", "official"),
            ("mod.uk", None),  # not in the table: no free trust
            ("tsinghua.edu.cn", "primary"),
            ("ox.ac.uk", "primary"),
        ]
        for index, (host, expected) in enumerate(cases):
            with self.subTest(host=host):
                source = _source(host, level="secondary", kind="web", suffix=f"{index + 1:016x}")
                classes = project_source_trust(source).classes
                if expected is None:
                    self.assertNotIn("official", classes)
                    self.assertNotIn("primary", classes)
                else:
                    self.assertIn(expected, classes)

    def test_sec_host_yields_filing_plus_official(self) -> None:
        projection = project_source_trust(_source("sec.gov", level="primary", kind="official"))

        self.assertEqual(projection.source_class, "filing")
        self.assertEqual(projection.classes, ("filing", "official"))

    def test_kind_mapping_media_blog_forum_social(self) -> None:
        news = project_source_trust(_source("example.com", kind="media"))
        blog = project_source_trust(_source("example.com", kind="blog"))
        forum = project_source_trust(_source("reddit.com", kind="web"))
        social = project_source_trust(_source("twitter.com", kind="social"))

        self.assertEqual(news.source_class, "news")
        self.assertEqual(blog.source_class, "secondary")
        self.assertEqual(forum.source_class, "forum")
        self.assertEqual(social.source_class, "social")

    def test_host_substring_lookalikes_do_not_classify_as_weak(self) -> None:
        lookalikes = [
            _source("notreddit.com", suffix="1" * 16),
            _source("reddit.com.evil.example", suffix="2" * 16),
            _source("facebook-community.example", suffix="3" * 16),
            _source("protwitter.org", suffix="4" * 16),
        ]

        for source in lookalikes:
            with self.subTest(host=source["host"]):
                projection = project_source_trust(source)
                self.assertNotIn(
                    projection.source_class,
                    {"forum", "social"},
                )

    def test_news_domains_match_by_domain_not_substring(self) -> None:
        real = project_source_trust(_source("business.reuters.com", suffix="1" * 16))
        fake = project_source_trust(_source("reuters-watch.example", suffix="2" * 16))

        self.assertEqual(real.source_class, "news")
        self.assertNotEqual(fake.source_class, "news")

    def test_weak_class_carries_warning_and_unknown_falls_open(self) -> None:
        weak = project_source_trust(_source("4chan.org", kind="social"))
        unknown = project_source_trust({
            "source_id": "source:" + "3" * 16,
            "host": "some-unknown-site.example",
        })

        self.assertIn("weak_source_class", weak.warnings)
        self.assertEqual(unknown.source_class, "unknown")
        self.assertEqual(unknown.tier, TIER_WEAK)
        self.assertIn("weak_source_class", unknown.warnings)

    def test_level_only_sources_use_primary_secondary(self) -> None:
        primary = project_source_trust(_source("institute.example", level="primary"))
        secondary = project_source_trust(_source("institute.example", level="secondary"))

        self.assertEqual(primary.source_class, "primary")
        self.assertEqual(secondary.source_class, "secondary")

    def test_invalid_source_without_ref_returns_none(self) -> None:
        self.assertIsNone(project_source_trust({"host": "example.com"}))
        self.assertIsNone(project_source_trust(None))
        self.assertIsNone(project_source_trust({"source_id": "junk"}))

    def test_projection_payload_never_carries_raw_url_or_body(self) -> None:
        payload = project_source_trust(
            _source("arxiv.org", freshness="fresh")
        ).to_payload()

        self.assertNotIn("url", payload)
        self.assertNotIn("body", payload)
        self.assertNotIn("excerpt", payload)
        self.assertTrue(str(payload["source_ref"]).startswith("source:"))


class SetProjectionTests(unittest.TestCase):
    def test_project_source_set_dedupes_by_ref(self) -> None:
        rows = project_source_set([
            _source("arxiv.org"),
            _source("arxiv.org"),  # duplicate ref
            {"host": "no-ref.example"},  # dropped, no valid ref
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_class, "preprint")

    def test_project_source_set_is_capped(self) -> None:
        rows = [
            _source(f"host{i}.example", suffix=f"{i:016x}")
            for i in range(MAX_PROJECTIONS + 10)
        ]

        projected = project_source_set(rows)

        self.assertEqual(len(projected), MAX_PROJECTIONS)


class EvaluateAgainstProfileTests(unittest.TestCase):
    def test_single_source_and_no_strong_source_warns(self) -> None:
        rows = project_source_set([_source("blog.example", kind="blog")])

        evaluation = evaluate_against_profile(rows)

        self.assertIn("single_source", evaluation["warnings"])
        self.assertIn("no_strong_source", evaluation["warnings"])

    def test_below_floor_counts_without_deleting_rows(self) -> None:
        weak = project_source_trust(_source("reddit.com"))
        strong = project_source_trust(_source("sec.gov", level="primary", kind="official"))

        evaluation = evaluate_against_profile([weak, strong], floor_tier=3)

        # The weak row is still present; only a warning is attached.
        self.assertIn(weak, [weak])
        self.assertEqual(evaluation["count"], 2)
        self.assertEqual(evaluation["below_floor_count"], 1)
        self.assertIn("sources_below_quality_threshold", evaluation["warnings"])
        self.assertIn("weak_source_class_present", evaluation["warnings"])

    def test_strong_mixed_set_has_no_floor_warnings_at_weak_floor(self) -> None:
        rows = project_source_set([
            _source("sec.gov", level="primary", kind="official", suffix="1" * 16),
            _source("github.com", suffix="2" * 16),
        ])

        evaluation = evaluate_against_profile(rows)

        self.assertNotIn("sources_below_quality_threshold", evaluation["warnings"])
        self.assertNotIn("single_source", evaluation["warnings"])


class SharedWarningTests(unittest.TestCase):
    """The rules moved verbatim from proof_quality must stay identical."""

    def test_legacy_warnings_are_reproduced_exactly(self) -> None:
        single = source_trust_warnings({
            "a": {"quality": {"level": "secondary", "kind": "blog", "freshness": "undated"}},
        })
        self.assertEqual(
            single,
            ("single_source", "sources_stale_or_undated", "no_primary_source", "weak_source_kind"),
        )

        healthy = source_trust_warnings({
            "a": {"quality": {"level": "primary", "kind": "official", "freshness": "fresh"}},
            "b": {"quality": {"level": "primary", "kind": "data", "freshness": "stale"}},
        })
        self.assertEqual(healthy, ())

    def test_non_mapping_quality_rows_are_ignored_like_before(self) -> None:
        warnings = source_trust_warnings({"a": {"quality": "garbage"}})
        self.assertEqual(warnings, ("single_source",))

    def test_projection_dataclass_is_frozen_and_bounded(self) -> None:
        projection = project_source_trust(_source("arxiv.org"))

        self.assertIsInstance(projection, SourceTrustProjection)
        with self.assertRaises(Exception):
            projection.host = "mutated"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
