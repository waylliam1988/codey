"""Catalog lifecycle: retirement, cache projections and explicit refresh."""
from __future__ import annotations

import copy
import threading
import time
from unittest.mock import Mock

import pytest

from codey.app import model_settings
from codey.app.context import AppContext
from codey.providers.zen import connection
from codey.providers.zen.catalog import ZenCatalog, parse_models
from codey.storage.local_store import write_json_atomic
from tests.test_zen_catalog_free_protocols_and_scoped_identity import directory


def listing():
    value = directory()
    value['opencode']['models']['retired'] = {
        **value['opencode']['models']['chat-free'], 'name': 'Retired model', 'status': 'deprecated',
    }
    return value


def test_deprecated_models_never_enter_cached_or_current_admission(tmp_path):
    value = listing()
    assert 'retired' not in {model.id for model in parse_models(value)}
    write_json_atomic(tmp_path / 'connections' / 'zen-catalog.json', {
        'schema_version': 2, 'directory': value, 'etag': 'old', 'updated': time.time(),
    })
    catalog = ZenCatalog(tmp_path, fetch=Mock(side_effect=AssertionError('fresh cache')))
    assert 'retired' not in {model.id for model in catalog.models}
    with pytest.raises(ValueError, match='deprecated|retired|no longer'):
        catalog.require('retired')
    with pytest.raises(ValueError):
        catalog.require_cached('retired')


def test_explicit_refresh_bypasses_ttl_but_keeps_conditional_etag_and_preferences(tmp_path, monkeypatch):
    fetch = Mock(return_value=(listing(), 'fixture'))
    catalog = ZenCatalog(tmp_path, fetch=fetch)
    catalog.refresh(force=True)
    monkeypatch.setattr(connection, 'DEFAULT_STATE_HOME', tmp_path)
    monkeypatch.setattr(connection, '_CATALOG', catalog)
    ctx = AppContext(tmp_path)
    try:
        before = ctx.providers.model_preferences.snapshot()
        fetch.return_value = (None, 'fixture')
        assert model_settings.discover_response(ctx, {'source': 'zen', 'force': False})[0] == 200
        assert fetch.call_count == 1
        status, result = model_settings.discover_response(ctx, {'source': 'zen', 'force': True})
        assert status == 200
        assert fetch.call_count == 2
        assert fetch.call_args.args[1] == 'fixture'
        assert result['source']['stale'] is False
        assert ctx.providers.model_preferences.snapshot() == before
    finally:
        ctx.close()


def test_settings_sanitizes_legacy_projection_without_network_or_preference_writes(tmp_path, monkeypatch):
    catalog = ZenCatalog(tmp_path, fetch=lambda *_: (listing(), 'fixture'))
    catalog.refresh(force=True)
    monkeypatch.setattr(connection, 'DEFAULT_STATE_HOME', tmp_path)
    monkeypatch.setattr(connection, '_CATALOG', catalog)
    catalog.fetch = Mock(side_effect=AssertionError('settings must be local'))
    ctx = AppContext(tmp_path)
    try:
        store = ctx.providers.model_preferences
        store.observe_catalog({'id': 'zen', 'models': [{'id': 'retired', 'name': 'Retired model'}]})
        before = store.path.read_bytes()
        status, result = model_settings.settings_response(ctx)
        source = next(source for source in result['sources'] if source['id'] == 'zen')
        assert status == 200
        assert 'retired' not in {model['id'] for model in source['models']}
        assert {'id': 'retired', 'name': 'Retired model', 'status': 'deprecated'} in source['unavailable_models']
        assert store.path.read_bytes() == before
    finally:
        ctx.close()


def test_failed_manual_refresh_preserves_last_good_models_and_reports_staleness(tmp_path, monkeypatch):
    catalog = ZenCatalog(tmp_path, fetch=lambda *_: (listing(), 'fixture'))
    catalog.refresh(force=True)
    monkeypatch.setattr(connection, 'DEFAULT_STATE_HOME', tmp_path)
    monkeypatch.setattr(connection, '_CATALOG', catalog)
    catalog.fetch = Mock(side_effect=OSError('fixture offline'))
    ctx = AppContext(tmp_path)
    try:
        status, result = model_settings.discover_response(ctx, {'source': 'zen'})
        assert status == 200
        assert result['source']['stale'] is True
        assert 'offline' in result['source']['error']
        assert {model['id'] for model in result['source']['models']} == {'chat-free', 'response-free'}
        assert ctx.providers.model_preferences.catalog('zen')['stale'] is True
    finally:
        ctx.close()


def test_retirement_changes_catalog_without_changing_selected_ids_or_frozen_selection(tmp_path, monkeypatch):
    value = directory()
    catalog = ZenCatalog(tmp_path, fetch=lambda *_: (copy.deepcopy(value), 'fixture'))
    catalog.refresh(force=True)
    monkeypatch.setattr(connection, 'DEFAULT_STATE_HOME', tmp_path)
    monkeypatch.setattr(connection, '_CATALOG', catalog)
    selection = connection.capture_selection({'model': 'chat-free'})
    value['opencode']['models']['chat-free']['status'] = 'deprecated'
    catalog.refresh(force=True)
    assert selection.model_id == 'chat-free'
    with pytest.raises(ValueError, match='deprecated|retired|no longer'):
        connection.capture_selection({'model': 'chat-free'})


def test_automatic_refresh_uses_five_minute_ttl_and_retries_failures_later(tmp_path, monkeypatch):
    from codey.providers.zen import catalog as module

    now = [1000.0]
    monkeypatch.setattr(module.time, 'time', lambda: now[0])
    fetch = Mock(return_value=(listing(), 'fixture'))
    catalog = ZenCatalog(tmp_path, fetch=fetch)
    catalog.refresh()
    now[0] += 299
    catalog.refresh()
    assert fetch.call_count == 1
    now[0] += 2
    fetch.side_effect = OSError('offline')
    catalog.refresh()
    assert fetch.call_count == 2 and catalog.stale
    catalog.refresh()
    assert fetch.call_count == 2
    now[0] += 301
    fetch.side_effect = None
    catalog.refresh()
    assert fetch.call_count == 3 and not catalog.stale


def test_explicit_catalog_supersedes_an_older_inflight_status_probe():
    from types import SimpleNamespace

    from codey.app.connection_status import ConnectionStatusCoordinator

    started, release = threading.Event(), threading.Event()
    def probe(_request):
        started.set()
        assert release.wait(5)
        return {'state': 'ready', 'payload': {'id': 'fixture', 'models': [{'id': 'retired'}]}}
    coordinator = ConnectionStatusCoordinator(
        provider_ids=lambda: ['fixture'], labels=lambda: {'fixture': 'Fixture'}, enabled=lambda _: True,
        connector=lambda _: SimpleNamespace(probe_status=probe),
    )
    try:
        coordinator.refresh('fixture')
        assert started.wait(5)
        future = coordinator._futures['fixture']
        fresh = {'id': 'fixture', 'models': []}
        coordinator.observe_catalog('fixture', fresh)
        release.set()
        future.result(timeout=5)
        assert coordinator.current('fixture').state == 'unavailable'
        assert coordinator.data('fixture')['payload'] == fresh
    finally:
        release.set()
        coordinator.close()


def test_disabled_automatic_discovery_is_read_only_but_manual_discovery_is_allowed(tmp_path, monkeypatch):
    catalog = ZenCatalog(tmp_path, fetch=Mock(return_value=(listing(), 'fixture')))
    monkeypatch.setattr(connection, 'DEFAULT_STATE_HOME', tmp_path)
    monkeypatch.setattr(connection, '_CATALOG', catalog)
    ctx = AppContext(tmp_path)
    try:
        assert not ctx.providers.model_preferences.enabled('zen')
        assert model_settings.discover_response(ctx, {'source': 'zen', 'force': False})[0] == 200
        catalog.fetch.assert_not_called()
        assert not ctx.providers.model_preferences.path.exists()
        assert model_settings.discover_response(ctx, {'source': 'zen', 'force': True})[0] == 200
        catalog.fetch.assert_called_once()
        assert not ctx.providers.model_preferences.enabled('zen')
    finally:
        ctx.close()


@pytest.mark.parametrize('force', [None, 1, 'false', []])
def test_refresh_rejects_non_boolean_force_before_discovery(tmp_path, force):
    ctx = AppContext(tmp_path)
    try:
        assert model_settings.discover_response(ctx, {'source': 'zen', 'force': force})[0] == 400
        assert not ctx.providers.model_preferences.path.exists()
    finally:
        ctx.close()
