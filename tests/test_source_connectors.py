from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

import pytest

from codey.research.identity import digest_text
from codey.research.redaction import (
    looks_secret_marker,
    looks_secret_shape,
    looks_sensitive_signal,
)
from codey.research.source_connectors import (
    FetchedSource,
    SourceHit,
    SourceConnectorRegistry,
    SourceConnectorSpec,
    built_in_connector_registry,
    connector_query_has_secret_signal,
    fetch_csv_tsv_file,
    fetch_json_file,
    fetch_local_file,
    fetch_recorded_hit,
    parse_arxiv_atom_fixture,
    parse_pubmed_fixture,
    safe_connector_query,
    safe_connector_query_terms,
    source_result_from_hits,
)
from codey.research.source_document import SourceDocument


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "research_connectors"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_builtin_connector_registry_is_stable_and_marks_minimum_shipped_fixtures() -> None:
    registry = built_in_connector_registry()

    assert registry.ids() == (
        "arxiv",
        "csv_tsv",
        "json_file",
        "local_file",
        "openalex",
        "pubmed",
        "rss",
    )
    assert {"local_file", "csv_tsv", "arxiv", "pubmed"}.issubset(registry.shipped_fixture_ids())
    assert registry.get("arxiv").status == "available"
    assert registry.get("pubmed").status == "available"
    assert "openalex" not in registry.shipped_fixture_ids()
    assert "rss" not in registry.shipped_fixture_ids()
    assert registry.get("openalex").status == "unavailable"
    assert registry.get("rss").status == "optional"


def test_connector_registry_does_not_import_runtime_browser_or_provider_layers() -> None:
    imports = _imports(ROOT / "codey" / "research" / "source_connectors.py")
    forbidden = {
        "codey.browser",
        "codey.providers",
        "codey.provider_controls",
        "codey.server",
        "codey.task_runner",
        "codey.tool_runtime",
        "codey.ghost",
    }

    assert forbidden.isdisjoint(imports), sorted(forbidden & imports)


def test_safe_connector_query_terms_drop_secret_url_and_path_spans() -> None:
    terms = safe_connector_query_terms(
        "clinical hepatotoxicity https://example.com/items?id=123 E:/docs/project",
    )

    joined = " ".join(terms)
    assert "clinical" in terms
    assert "hepatotoxicity" in terms
    assert "example" not in joined
    assert "project" not in joined


def test_safe_connector_query_terms_mask_secret_marker_value_windows() -> None:
    cases = (
        "api key abcdef clinical cancer",
        "api key is abcdef clinical cancer",
        "api key named abcdef clinical cancer",
        "api key called livekey clinical cancer",
        "api_key=abcdef clinical cancer",
        "password hunter2 clinical cancer",
        "password: hunter2 clinical cancer",
        "password is hunter2 clinical cancer",
        "password is equal to hunter2 clinical cancer",
        "password is set to hunter2 clinical cancer",
        "password is configured as hunter2 clinical cancer",
        "password is configured as known as called livekey clinical cancer",
        "password - is - configured - as - known - as - called - livekey clinical cancer",
        "password . is . configured . as . known . as . called . livekey clinical cancer",
        "api key — called — livekey clinical cancer",
        "client_secret = abcdef clinical cancer",
        "client_secret is mysecretvalue clinical cancer",
        "client secret known as abcdef clinical cancer",
        "private key is topsecret clinical cancer",
        "Authorization: Bearer abcdef clinical cancer",
    )

    for query in cases:
        safe = safe_connector_query(query)
        joined = " ".join(safe.terms).casefold()

        assert "clinical" in safe.terms
        assert "cancer" in safe.terms
        assert safe.redacted
        assert safe.skip_reason == ""
        assert "api" not in joined
        assert "key" not in joined
        assert "password" not in joined
        assert "client" not in joined
        assert "secret" not in joined
        assert "authorization" not in joined
        assert "bearer" not in joined
        assert "configured" not in joined
        assert "known" not in joined
        assert "called" not in joined
        assert "abcdef" not in joined
        assert "livekey" not in joined
        assert "hunter2" not in joined
        assert "mysecretvalue" not in joined
        assert "topsecret" not in joined


def test_safe_connector_query_terms_mask_chinese_secret_marker_value_windows() -> None:
    cases = (
        "密码 是 hunter2 临床 癌症",
        "密码 hunter2 临床 癌症",
        "密钥 abcdef 临床 癌症",
        "密钥等于 abcdef clinical",
        "访问令牌 设置为 abcdef clinical cancer",
        "私钥 名为 livekey clinical cancer",
    )

    for query in cases:
        safe = safe_connector_query(query)
        joined = " ".join(safe.terms).casefold()

        assert safe.terms
        assert safe.redacted
        assert safe.skip_reason == ""
        assert "abcdef" not in joined
        assert "hunter2" not in joined
        assert "livekey" not in joined
        assert "等于" not in joined


def test_redaction_markers_are_boundary_aware_for_scientific_terms() -> None:
    assert not looks_sensitive_signal("secreted insulin secretion pathway")
    assert not looks_secret_marker("secretion")
    assert looks_secret_marker("secret_token")
    assert looks_secret_marker("api key")
    assert looks_secret_shape("sk-" + "A" * 16)


def test_connector_query_secret_signal_detects_separator_split_markers() -> None:
    sensitive = (
        "api - key - abcdef clinical cancer",
        "api:key abcdef clinical cancer",
        "private - key topsecret clinical cancer",
        "access + key abcdef clinical cancer",
        "password . is . called . livekey clinical cancer",
        "password hunter2 clinical cancer",
        "client · secret abcdef clinical cancer",
        "密 - 钥 abcdef 临床 癌症",
        "密钥 abcdef 临床 癌症",
        "密码 hunter2 临床 癌症",
    )

    for query in sensitive:
        assert connector_query_has_secret_signal(query)

    assert not connector_query_has_secret_signal("secreted insulin secretion pathway clinical cancer")
    assert not connector_query_has_secret_signal("token efficient transformers benchmark")
    assert not connector_query_has_secret_signal("authorization trial consent policy")
    assert not connector_query_has_secret_signal("secret sharing cryptography benchmark")
    assert safe_connector_query("token efficient transformers benchmark").terms
    assert safe_connector_query("authorization trial consent policy").terms
    assert safe_connector_query("secret sharing cryptography benchmark").terms


def test_safe_connector_query_terms_keep_secreted_science_terms() -> None:
    terms = safe_connector_query_terms("secreted insulin secretion pathway")

    assert terms == ("secreted", "insulin", "secretion", "pathway")


def test_safe_connector_query_terms_keep_scientific_slash_terms() -> None:
    terms = safe_connector_query_terms(
        "TGF-beta/SMAD signaling JAK/STAT pathway p53/MDM2 interaction IL-6/JAK/STAT "
        "https://example.com/a/b E:/secret/project",
    )

    assert "TGF-beta/SMAD" in terms
    assert "JAK/STAT" in terms
    assert "p53/MDM2" in terms
    assert "IL-6/JAK/STAT" in terms
    joined = " ".join(terms)
    assert "example" not in joined
    assert "secret" not in joined.casefold()


def test_safe_connector_query_terms_drop_path_like_slash_tokens() -> None:
    terms = safe_connector_query_terms(
        "docs/ADR2026/research Docs/ADR/Plan ProjectX/ConfigV2 and JAK/STAT pathway "
        "src/Foo2/bar TGF-beta/SMAD IL-6/JAK/STAT"
    )

    joined = " ".join(terms)
    assert "docs/ADR2026/research" not in joined
    assert "Docs/ADR/Plan" not in joined
    assert "ProjectX/ConfigV2" not in joined
    assert "src/Foo2/bar" not in joined
    assert "JAK/STAT" in terms
    assert "TGF-beta/SMAD" in terms
    assert "IL-6/JAK/STAT" in terms


def test_local_file_fetch_is_confined_to_allowed_roots_and_payload_hides_raw_path() -> None:
    with tempfile.TemporaryDirectory() as root_td, tempfile.TemporaryDirectory() as outside_td:
        root = Path(root_td)
        outside = Path(outside_td)
        source = root / "nested" / "secret_note.txt"
        source.parent.mkdir()
        source.write_text("local evidence body", encoding="utf-8")
        outside_file = outside / "escape.txt"
        outside_file.write_text("outside", encoding="utf-8")

        fetched = fetch_local_file("nested/secret_note.txt", allowed_roots=(root,))
        payload = fetched.to_payload()
        serialized = json.dumps(payload, ensure_ascii=False)

        assert fetched.document.text == "local evidence body"
        assert fetched.source_ref.startswith("source_ref:")
        assert fetched.source_id.startswith("connector_source:")
        assert payload["fetched"] is True
        assert payload["evidence_ready"] is False
        assert str(root) not in serialized
        assert str(source) not in serialized
        assert "local evidence body" not in serialized
        with pytest.raises(ValueError, match="escapes allowed roots"):
            fetch_local_file(outside_file, allowed_roots=(root,))


def test_csv_tsv_and_json_fixtures_fetch_stable_documents_with_standard_parsing() -> None:
    csv_fetched = fetch_csv_tsv_file(FIXTURES / "table.csv", allowed_roots=(FIXTURES,))
    tsv_fetched = fetch_csv_tsv_file(FIXTURES / "table.tsv", allowed_roots=(FIXTURES,))
    json_fetched = fetch_json_file(FIXTURES / "data.json", allowed_roots=(FIXTURES,))

    assert "alpha, beta" in csv_fetched.document.text
    assert "delimiter: ," in csv_fetched.document.text
    assert "delimiter: \\t" in tsv_fetched.document.text
    assert '"dataset": "fixture"' in json_fetched.document.text
    assert csv_fetched.source_ref == fetch_csv_tsv_file(FIXTURES / "table.csv", allowed_roots=(FIXTURES,)).source_ref
    assert json_fetched.source_ref.startswith("source_ref:")


def test_csv_tsv_truncation_only_marks_when_extra_rows_exist() -> None:
    exact = fetch_csv_tsv_file(FIXTURES / "table.csv", allowed_roots=(FIXTURES,), max_rows=3)
    truncated = fetch_csv_tsv_file(FIXTURES / "table.csv", allowed_roots=(FIXTURES,), max_rows=2)

    assert exact.document.truncated is False
    assert "rows_truncated" not in exact.warnings
    assert truncated.document.truncated is True
    assert "rows_truncated" in truncated.warnings
    assert "label=beta" not in truncated.document.text


def test_arxiv_recorded_fixture_produces_stable_source_refs_without_raw_url_payload() -> None:
    fixture = (FIXTURES / "arxiv_atom.xml").read_text(encoding="utf-8")

    first = parse_arxiv_atom_fixture(fixture, query="retrieval augmented generation")
    second = parse_arxiv_atom_fixture(fixture, query="retrieval augmented generation")
    hit = first.hits[0]
    payload = first.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert hit.connector_id == "arxiv"
    assert hit.source_ref == second.hits[0].source_ref
    assert hit.source_id == second.hits[0].source_id
    assert hit.hit_id == second.hits[0].hit_id
    assert hit.source_ref.startswith("source_ref:")
    assert hit.source_id.startswith("connector_source:")
    assert hit.hit_id.startswith("source_hit:")
    assert payload["hit_count"] == 1
    assert "https://arxiv.org/abs/2401.01234v1" not in serialized
    assert "Retrieval Augmented Generation" not in serialized


def test_arxiv_recorded_fixture_rejects_non_arxiv_hosts_and_bad_limit_is_bounded() -> None:
    fixture = (FIXTURES / "arxiv_atom.xml").read_text(encoding="utf-8")
    wrong_host = fixture.replace("https://arxiv.org/abs/2401.01234v1", "https://example.com/abs/2401.01234v1")

    result = parse_arxiv_atom_fixture(wrong_host, query="retrieval augmented generation", limit="bad")  # type: ignore[arg-type]
    bounded = parse_arxiv_atom_fixture(fixture, limit="bad")  # type: ignore[arg-type]

    assert result.hits == ()
    assert len(bounded.hits) == 1


def test_arxiv_and_pubmed_recorded_fixtures_reject_malformed_ids_and_use_safe_query_digest() -> None:
    arxiv_fixture = (FIXTURES / "arxiv_atom.xml").read_text(encoding="utf-8").replace(
        "https://arxiv.org/abs/2401.01234v1",
        "https://arxiv.org/abs/SECRET_TOKEN",
    )
    pubmed_fixture = (FIXTURES / "pubmed.xml").read_text(encoding="utf-8").replace(
        ">12345678</PMID>",
        ">SECRET_TOKEN</PMID>",
    )
    secret_query = (
        "clinical hepatotoxicity SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWX "
        "https://example.com/items?token=SECRET E:/secret/project"
    )

    arxiv = parse_arxiv_atom_fixture(arxiv_fixture, query=secret_query)
    pubmed = parse_pubmed_fixture(pubmed_fixture, query=secret_query)
    result = source_result_from_hits("pubmed", query=secret_query, hits=[])

    assert arxiv.hits == ()
    assert pubmed.hits == ()
    assert arxiv.query_digest == digest_text("clinical hepatotoxicity")
    assert pubmed.query_digest == digest_text("clinical hepatotoxicity")
    assert result.query_digest == digest_text("clinical hepatotoxicity")
    assert arxiv.query_digest != digest_text(secret_query)
    assert pubmed.query_digest != digest_text(secret_query)


def test_pubmed_recorded_fixture_fetches_openable_document_but_hit_is_not_evidence() -> None:
    fixture = (FIXTURES / "pubmed.xml").read_text(encoding="utf-8")

    result = parse_pubmed_fixture(fixture, query="immune checkpoint inhibitor hepatotoxicity", limit="bad")  # type: ignore[arg-type]
    hit = result.hits[0]
    fetched = fetch_recorded_hit(hit)
    hit_payload = hit.to_payload()
    fetched_payload = fetched.to_payload()
    serialized = json.dumps({"hit": hit_payload, "fetched": fetched_payload}, ensure_ascii=False)

    assert hit.connector_id == "pubmed"
    assert "pmid:12345678" in hit.metadata_refs
    assert fetched.document.final_url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert "hepatotoxicity" in fetched.document.text
    assert hit_payload["evidence_ready"] is False
    assert fetched_payload["evidence_ready"] is False
    assert "evidence_id" not in hit_payload
    assert "Immune checkpoint" not in serialized
    assert "hepatotoxicity" not in serialized


def test_source_hit_payload_filters_sensitive_metadata_refs() -> None:
    hit = SourceHit(
        connector_id="pubmed",
        hit_id="source_hit:" + "a" * 16,
        source_ref="source_ref:" + "b" * 16,
        source_id="connector_source:" + "c" * 16,
        title="metadata",
        snippet="metadata",
        metadata_refs=(
            "pmid:12345678",
            "SECRET_TOKEN",
            "sk-" + "a" * 24,
            "client_secret",
        ),
    )

    payload = hit.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["metadata_refs"] == ["pmid:12345678"]
    assert "SECRET_TOKEN" not in serialized
    assert "sk-" not in serialized
    assert "client_secret" not in serialized


def test_source_hit_payload_filters_malformed_sensitive_scalar_fields() -> None:
    hit = SourceHit(
        connector_id="pubmed",
        hit_id="source_hit:" + "a" * 16,
        source_ref="source_ref:" + "b" * 16,
        source_id="connector_source:" + "c" * 16,
        title="metadata",
        snippet="metadata",
        content_kind="SECRET_TOKEN",
        source_kind="client_secret",
        published_at="SECRET_TOKEN",
    )

    payload = hit.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["content_kind"] == "text"
    assert payload["source_kind"] == ""
    assert "published_at" not in payload
    assert "SECRET_TOKEN" not in serialized
    assert "client_secret" not in serialized


def test_fetched_source_payload_allow_lists_document_scalar_fields() -> None:
    fetched = FetchedSource.from_document(
        connector_id="pubmed",
        source_ref="source_ref:" + "a" * 16,
        source_id="connector_source:" + "b" * 16,
        document=SourceDocument(
            requested_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
            final_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
            title="metadata",
            content_kind="SECRET_TOKEN",
            mime_type="SECRET_MIME",
            text="document body",
        ),
    )

    payload = fetched.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["content_kind"] == "text"
    assert payload["mime_type"] == ""
    assert "SECRET_TOKEN" not in serialized
    assert "SECRET_MIME" not in serialized
    assert "document body" not in serialized


def test_fetched_source_payload_filters_sensitive_warnings() -> None:
    fetched = FetchedSource.from_document(
        connector_id="pubmed",
        source_ref="source_ref:" + "a" * 16,
        source_id="connector_source:" + "b" * 16,
        document=SourceDocument(
            requested_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
            final_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
            title="metadata",
            text="document body",
        ),
        warnings=("text_truncated", "SECRET_CLIENT_NAME", "authorization_required", "sk-" + "a" * 24),
    )

    payload = fetched.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["warnings"] == ["text_truncated", "authorization_required"]
    assert "SECRET_CLIENT_NAME" not in serialized
    assert "sk-" not in serialized


def test_source_connector_spec_payload_filters_sensitive_catalog_codes() -> None:
    spec = SourceConnectorSpec(
        id="pubmed",
        kind="biomedical_literature",
        status="available",
        source_quality_hint={
            "level": "primary source",
            "client_secret": "SECRET_CLIENT_NAME",
            "apiKey": "visible",
            "token_budget": "token_budget_exceeded",
            "密码": "hunter2",
        },
        failure_modes=(
            "rate_limit",
            "authorization_required",
            "SECRET_CLIENT_NAME",
            "client_secret",
            "密码",
            "sk-" + "a" * 24,
        ),
    )

    payload = spec.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["source_quality_hint"] == {
        "level": "primary_source",
        "token_budget": "token_budget_exceeded",
    }
    assert payload["failure_modes"] == ["rate_limit", "authorization_required"]
    assert "SECRET_CLIENT_NAME" not in serialized
    assert "client_secret" not in serialized
    assert "apiKey" not in serialized
    assert "密码" not in serialized
    assert "sk-" not in serialized


def test_source_connector_spec_payload_filters_noncanonical_catalog_codes() -> None:
    payload = SourceConnectorSpec(
        id="PubMed",
        kind="biomedical_literature ",
    ).to_payload()

    assert payload["id"] == ""
    assert payload["kind"] == ""


def test_source_connector_registry_rejects_sensitive_custom_id_or_kind() -> None:
    with pytest.raises(ValueError, match="connector id must be canonical non-sensitive snake_case"):
        SourceConnectorRegistry((
            SourceConnectorSpec(id="client_secret", kind="biomedical_literature"),
        ))
    with pytest.raises(ValueError, match=r"pubmed\.kind must be canonical non-sensitive snake_case"):
        SourceConnectorRegistry((
            SourceConnectorSpec(id="pubmed", kind="client_secret"),
        ))


def test_source_connector_registry_rejects_noncanonical_custom_id_or_kind() -> None:
    with pytest.raises(ValueError, match="connector id must be canonical non-sensitive snake_case"):
        SourceConnectorRegistry((
            SourceConnectorSpec(id="PubMed", kind="biomedical_literature"),
        ))
    with pytest.raises(ValueError, match=r"pubmed\.kind must be canonical non-sensitive snake_case"):
        SourceConnectorRegistry((
            SourceConnectorSpec(id="pubmed", kind="Biomedical_Literature"),
        ))


def test_source_connector_result_payload_filters_sensitive_warnings_and_errors() -> None:
    result = source_result_from_hits(
        "pubmed",
        warnings=("rate_limit", "SECRET_CLIENT_NAME", "authorization_required", "密钥", "sk-" + "a" * 24),
        errors=("fixture_parse_failed", "client_secret", "apiKey", "sk-" + "b" * 24),
    )

    payload = result.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["warnings"] == ["rate_limit", "authorization_required"]
    assert payload["errors"] == ["fixture_parse_failed"]
    assert "SECRET_CLIENT_NAME" not in serialized
    assert "client_secret" not in serialized
    assert "apiKey" not in serialized
    assert "密钥" not in serialized
    assert "sk-" not in serialized


def test_recorded_hit_fetch_rejects_private_or_local_urls() -> None:
    hit = SourceHit(
        connector_id="pubmed",
        hit_id="source_hit:" + "a" * 16,
        source_ref="source_ref:" + "b" * 16,
        source_id="connector_source:" + "c" * 16,
        canonical_url="http://localhost/123",
        title="local",
        snippet="local",
    )

    with pytest.raises(ValueError, match="local|non-public|http"):
        fetch_recorded_hit(hit)


def test_recorded_hit_fetch_rejects_wrong_public_host_for_connector() -> None:
    hit = SourceHit(
        connector_id="pubmed",
        hit_id="source_hit:" + "a" * 16,
        source_ref="source_ref:" + "b" * 16,
        source_id="connector_source:" + "c" * 16,
        canonical_url="https://example.com/12345678/",
        title="wrong host",
        snippet="wrong host",
    )

    with pytest.raises(ValueError, match="connector URL host is not allowed"):
        fetch_recorded_hit(hit)


def test_recorded_hit_fetch_rejects_invalid_connector_url_ids() -> None:
    pubmed = SourceHit(
        connector_id="pubmed",
        hit_id="source_hit:" + "a" * 16,
        source_ref="source_ref:" + "b" * 16,
        source_id="connector_source:" + "c" * 16,
        canonical_url="https://pubmed.ncbi.nlm.nih.gov/SECRET_TOKEN/",
        title="bad pmid",
        snippet="bad pmid",
    )
    arxiv = SourceHit(
        connector_id="arxiv",
        hit_id="source_hit:" + "d" * 16,
        source_ref="source_ref:" + "e" * 16,
        source_id="connector_source:" + "f" * 16,
        canonical_url="https://arxiv.org/abs/SECRET_TOKEN",
        title="bad arxiv",
        snippet="bad arxiv",
    )

    with pytest.raises(ValueError, match="invalid PMID"):
        fetch_recorded_hit(pubmed)
    with pytest.raises(ValueError, match="invalid arXiv ID"):
        fetch_recorded_hit(arxiv)
