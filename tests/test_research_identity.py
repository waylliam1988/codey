from __future__ import annotations

import json
import tempfile
from pathlib import Path

from codey.refs import content_digest, digest_text, stable_ref
from codey.research.identity import (
    path_ref,
    project_ref,
    sanitize_research_url_ref,
)


def test_url_ref_redacts_query_values_keys_and_malformed_userinfo_before_digest() -> None:
    first = sanitize_research_url_ref(
        "https://user:FIRST@example.com/items?FIRST_SECRET=1&client_secret=FIRST"
    )
    second = sanitize_research_url_ref(
        "https://user:SECOND@example.com/items?SECOND_SECRET=1&client_secret=SECOND"
    )
    no_host_first = sanitize_research_url_ref("/items?session_id=FIRST&ok=public")
    no_host_second = sanitize_research_url_ref("/items?session_id=SECOND&ok=public")
    malformed_first = sanitize_research_url_ref("https://user:PASS_A@[bad")
    malformed_second = sanitize_research_url_ref("https://user:PASS_B@[bad")
    serialized = json.dumps(
        {
            "first": first,
            "second": second,
            "no_host_first": no_host_first,
            "no_host_second": no_host_second,
            "malformed_first": malformed_first,
            "malformed_second": malformed_second,
        },
        ensure_ascii=False,
    )

    assert first["host"] == "example.com"
    assert first["redacted"] is True
    assert second["redacted"] is True
    assert first["url_digest"] == second["url_digest"]
    assert no_host_first["url_digest"] == no_host_second["url_digest"]
    assert malformed_first["url_digest"] == malformed_second["url_digest"]
    assert "FIRST" not in serialized
    assert "SECOND" not in serialized
    assert "PASS_A" not in serialized
    assert "PASS_B" not in serialized


def test_path_and_project_refs_keep_basenames_without_raw_absolute_paths() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        path = project / "data" / "sample.csv"
        path.parent.mkdir(parents=True)
        path.write_text("value\n", encoding="utf-8")

        project_payload = project_ref(project)
        path_payload = path_ref(path, project=project)
        serialized = json.dumps(
            {"project": project_payload, "path": path_payload},
            ensure_ascii=False,
        )

    assert project_payload["basename"] == "project"
    assert path_payload["basename"] == "sample.csv"
    assert str(project) not in serialized
    assert str(path) not in serialized
    assert project_payload["digest"].startswith("sha256:")
    assert path_payload["digest"].startswith("sha256:")


def test_stable_ref_and_digest_text_are_deterministic_and_prefixed() -> None:
    assert digest_text("hello") == digest_text("hello")
    assert digest_text("hello") != digest_text("world")
    assert stable_ref("evidence ledger", "a", 1) == stable_ref("evidence ledger", "a", 1)
    assert stable_ref("evidence ledger", "a", 1).startswith("evidence_ledger:")


def test_content_digest_only_accepts_real_sha256_hex_strings() -> None:
    valid = "sha256:" + "A" * 64
    pseudo = "sha256:" + "SECRET".ljust(64, "X")

    valid_ref = content_digest(valid)
    pseudo_ref = content_digest(pseudo)

    assert valid_ref == "sha256:" + "a" * 64
    assert pseudo_ref.startswith("sha256:")
    assert pseudo_ref != pseudo
    assert "SECRET" not in pseudo_ref
