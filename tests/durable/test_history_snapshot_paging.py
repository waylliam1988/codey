"""Older pages remain pinned while the live task appends to the same branch."""
import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore


@pytest.fixture(params=['memory', 'sqlite'])
def session(request, tmp_path):
    session = DurableSession(MemoryDurableStore() if request.param == 'memory' else SQLiteDurableStore(tmp_path / 'state.sqlite3'))
    session.create_conversation('chat')
    for index in range(10):
        session.append_message('chat', 'user_message', {'text': f'old {index}'})
    yield session
    session.close()


def test_loading_earlier_keeps_a_stable_snapshot_during_continuous_appends(session):
    page = session.history_page('chat', limit=2)
    revision = page['snapshot_revision']
    items = page['items']
    pages = 1
    while page['has_more']:
        session.append_message('chat', 'assistant_message', {'text': f'live update {pages}'})
        page = session.history_page('chat', before=page['before_cursor'], limit=2)
        assert page['snapshot_revision'] == revision
        items = page['items'] + items
        pages += 1
        assert pages <= 5
    assert [item['text'] for item in items] == [f'old {index}' for index in range(10)]
    assert len({item['id'] for item in items}) == 10
    assert session.history_page('chat')['snapshot_revision'] > revision


def test_a_branch_change_invalidates_the_pinned_cursor_instead_of_mixing_paths(session):
    page = session.history_page('chat', limit=2)
    first = session.history_page('chat')['items'][0]['id']
    session.branch('chat', first)
    session.append_message('chat', 'assistant_message', {'text': 'different branch'})
    with pytest.raises(ValueError, match='history snapshot changed'):
        session.history_page('chat', before=page['before_cursor'], limit=2)
