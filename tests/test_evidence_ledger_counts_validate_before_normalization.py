"""Ledger schema validation must inspect the original persisted count."""

from copy import deepcopy

import pytest

from codey.research import evidence_ledger as ledger
from codey.research.evidence_ledger import EvidenceLedgerStore
from tests.test_research_evidence_ledger import _wide_record


@pytest.mark.parametrize(
    "field,validator,base",
    [
        (
            "page_count",
            ledger._source_schema_ok,
            {
                "source_id": "s1",
                "host": "example.com",
                "title_digest": "",
                "content_hash": "sha256:" + "a" * 64,
                "retrieved_at": "",
                "content_kind": "web",
                "pages_read": [],
                "truncated": False,
            },
        ),
        (
            "claim_chars",
            ledger._claim_schema_ok,
            {
                "claim_id": "c1",
                "claim_text_digest": "sha256:" + "a" * 64,
                "claim_section": "conclusion",
                "citation_numbers": [],
                "evidence_refs": [],
                "assumption_refs": [],
                "status": "evidence_backed",
            },
        ),
        (
            "assumption_chars",
            ledger._assumption_schema_ok,
            {"assumption_id": "a1", "assumption_text_digest": "sha256:" + "a" * 64, "reason": "", "claim_ref": ""},
        ),
    ],
)
@pytest.mark.parametrize("value", [-1, True, False])
def test_invalid_count_cannot_be_laundered_by_clamping(field, validator, base, value):
    valid = {**base, field: 0}
    assert validator(valid), "the counterexample must start from a valid row"
    invalid = {**base, field: value}
    before = deepcopy(invalid)
    assert validator(invalid) is False
    assert invalid == before


def test_persisted_canonical_schema_rejects_negative_source_count(tmp_path):
    store = EvidenceLedgerStore(tmp_path)
    result = store.append_record(_wide_record(1, evidence_count=1), session_id="session-ledger")
    assert result.ok
    snapshot = store.load(session_id="session-ledger")
    assert snapshot.available
    payload = deepcopy(snapshot.payload)
    next(iter(payload["sources"].values()))["page_count"] = -1
    assert ledger._canonical_ledger_payload(payload) is False


@pytest.mark.parametrize("value", [True, 1.0])
@pytest.mark.parametrize("table,field", [("sources", "pages_read"), ("claims", "citation_numbers")])
def test_canonical_number_lists_require_exact_integers(tmp_path, value, table, field):
    store = EvidenceLedgerStore(tmp_path)
    assert store.append_record(_wide_record(1, evidence_count=1), session_id="session-ledger").ok
    payload = deepcopy(store.load(session_id="session-ledger").payload)
    row = next(iter(payload[table].values()))
    row[field] = [1]
    assert ledger._canonical_ledger_payload(payload)
    row[field] = [value]
    assert ledger._canonical_ledger_payload(payload) is False


@pytest.mark.parametrize("field,value", [("sources", True), ("assumptions", False), ("unsupported_claims", False)])
def test_record_counts_cannot_use_boolean_integers(tmp_path, field, value):
    store = EvidenceLedgerStore(tmp_path)
    assert store.append_record(_wide_record(1, evidence_count=1), session_id="session-ledger").ok
    payload = deepcopy(store.load(session_id="session-ledger").payload)
    assert ledger._canonical_ledger_payload(payload)
    payload["records"][0]["counts"][field] = value
    assert ledger._canonical_ledger_payload(payload) is False
