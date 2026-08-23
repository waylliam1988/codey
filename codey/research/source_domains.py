"""Registered-shape host domain tables for source classification.

Single owner of the domain data used to decide what kind of source a URL
belongs to. Both the ledger's quality classifier (which stamps
``SourceQuality`` at capture time) and the source-trust projection (which
derives trust classes from facts already on the source) read these tables,
so the two layers can never drift back into different rules.

Matching is exact-or-dot-suffix only (``host_matches``): substring tests
like ``".gov." in host`` would let attacker-owned lookalikes such as
``sec.gov.evil.example`` inherit government trust. Compound suffixes are a
finite, stable list of registered public-suffix shapes; unknown ones simply
do not get free trust.
"""

from __future__ import annotations

from codey.refs import is_valid_hostname as _is_valid_hostname


GOV_SUFFIXES = (
    "gov",
    "mil",
    "gov.au",
    "gov.br",
    "gov.cn",
    "gov.in",
    "gov.uk",
    "gov.za",
    "gouv.fr",
    "gc.ca",
    "go.jp",
    "go.id",
)
EDU_SUFFIXES = (
    "edu",
    "edu.au",
    "edu.cn",
    "edu.hk",
    "ac.uk",
)
PREPRINT_HOSTS = ("arxiv.org", "biorxiv.org", "medrxiv.org", "ssrn.com")
PEER_REVIEWED_HOSTS = (
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
    "doi.org",
    "ieeexplore.ieee.org",
    "sciencedirect.com",
    "link.springer.com",
    "nature.com",
    "science.org",
    "dl.acm.org",
    "jmlr.org",
    "plos.org",
    "frontiersin.org",
)
REPO_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
FILING_HOSTS = ("sec.gov", "finra.org", "edi.gov")
STANDARD_HOSTS = ("iso.org", "ieee.org", "ietf.org", "ansi.org", "w3.org", "itu.int")
DATASET_HOSTS = (
    "data.gov",
    "data.nasa.gov",
    "data.europa.eu",
    "zenodo.org",
    "figshare.com",
    "kaggle.com",
    "archive.ics.uci.edu",
)
NEWS_HOSTS = (
    "apnews.com",
    "ap.org",
    "bbc.co.uk",
    "bbc.com",
    "bloomberg.com",
    "cnbc.com",
    "ft.com",
    "nytimes.com",
    "reuters.com",
    "theguardian.com",
    "wsj.com",
)
BLOG_HOSTS = ("medium.com", "substack.com", "blogspot.com", "wordpress.com")
FORUM_HOSTS = (
    "news.ycombinator.com",
    "quora.com",
    "reddit.com",
    "stackoverflow.com",
    "stackexchange.com",
    "v2ex.com",
    "zhihu.com",
)
SOCIAL_HOSTS = (
    "facebook.com",
    "linkedin.com",
    "weibo.com",
    "x.com",
    "twitter.com",
)


def strip_www(host: str) -> str:
    return str(host or "").strip().lower().removeprefix("www.")


def host_matches(host: str, domain: str) -> bool:
    """Exact or dot-suffix domain match; never a substring test."""

    return host == domain or host.endswith("." + domain)


def matches_any(host: str, domains) -> bool:
    """Fail-closed domain match.

    Malformed hostnames (empty labels, doubled dots, single labels, bad
    characters) never match any table, so garbage input can neither inherit
    trust nor crash a suffix comparison.
    """

    lowered = strip_www(host)
    if not lowered or not _is_valid_hostname(lowered):
        return False
    return any(host_matches(lowered, str(domain).lower()) for domain in domains)


def is_government_host(host: str) -> bool:
    return matches_any(host, GOV_SUFFIXES)


def is_education_host(host: str) -> bool:
    return matches_any(host, EDU_SUFFIXES)


__all__ = [
    "BLOG_HOSTS",
    "DATASET_HOSTS",
    "EDU_SUFFIXES",
    "FILING_HOSTS",
    "FORUM_HOSTS",
    "GOV_SUFFIXES",
    "NEWS_HOSTS",
    "PEER_REVIEWED_HOSTS",
    "PREPRINT_HOSTS",
    "REPO_HOSTS",
    "SOCIAL_HOSTS",
    "STANDARD_HOSTS",
    "host_matches",
    "is_education_host",
    "is_government_host",
    "matches_any",
    "strip_www",
]
