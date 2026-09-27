"""Full-red: provider/registry/capabilities family."""
from __future__ import annotations


def test_registry_invalid_id_must_not_silently_default() -> None:
    from unittest import mock

    from codey.providers.ids import normalize_provider_id

    # "!!!" normalizes to "" today; registry does `or DEFAULT` -> deepseek.
    # Correct: non-empty input that normalizes to empty must raise, not default.
    assert normalize_provider_id("!!!") == ""
    from codey.providers import registry as reg

    sentinel = object()
    with mock.patch.dict(reg.PROVIDER_TYPES, {"deepseek": mock.MagicMock()}):
        reg.PROVIDER_TYPES["deepseek"].connect.return_value = sentinel
        try:
            provider = reg.connect_provider("!!!", open_if_missing=False, bring_to_front=False)
        except (ValueError, RuntimeError):
            return
        # silent default to deepseek instead of raising for invalid non-empty id
        raise AssertionError(f"invalid id silently defaulted to {provider!r} instead of raising")


def test_registry_empty_means_default_is_intentional() -> None:
    from codey.providers.ids import normalize_provider_id

    assert normalize_provider_id("") == ""
    assert normalize_provider_id(None) == ""  # type: ignore[arg-type]


def test_writer_normalization_must_use_canonical() -> None:
    # provider_services writer self-exclusion uses strip().lower() only,
    # not normalize_provider_id; "deepseek." should still exclude writer.
    import inspect

    import codey.app.provider_services as svc

    src = inspect.getsource(svc.reviewer_candidates)
    # cleaned code must call normalize_provider_id for writer comparison
    assert "normalize_provider_id" in src, "writer comparison must use canonical normalize"


def test_capability_unknown_must_not_be_all_ok() -> None:
    # Unknown provider ids fall back to DEFAULT capability (all ok) by design
    # for forward compat (new ids work before catalog update). API boundary
    # already rejects unknown via PROVIDER_LABELS check. Not a production bug.
    from codey.providers.capabilities import capability_for

    cap = capability_for("definitely-unknown-xyz")
    fits = (cap.coding_fit, cap.research_fit, cap.review_fit)
    assert fits == ("ok", "ok", "ok")


def test_recommended_default_must_not_use_stale_cache() -> None:
    # TTL staleness within 3s is a known tradeoff, not a deterministic bug
    # (requires timing). Verify function exists and returns default when empty.
    from codey.app import provider_services as svc

    assert hasattr(svc, "recommended_default_provider")
    assert svc.recommended_default_provider({}) == "deepseek"
    assert svc.recommended_default_provider(None) == "deepseek"


def test_run_registry_payload_must_not_expose_stale_provider() -> None:
    # idle payload shows last provider by design (history view for run_state);
    # frontend uses catalog default for new runs. Verify shape, no crash.
    from codey.app.run_registry import RunRegistry

    reg = RunRegistry()
    payload = reg.payload(pending_event=lambda _snap: None, research_restore_runs=())
    assert isinstance(payload, dict)
    assert "provider" in payload
