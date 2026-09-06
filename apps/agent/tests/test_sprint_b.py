"""Unit tests Sprint B: session resumption (get/save handle).

Tidak ada network call — hanya SQLite di tmp path.
"""

import agent_gemini_live as agl


def test_save_and_get_session_handle(monkeypatch, tmp_path):
    """Round-trip: save → retrieve handle dari SQLite."""
    db_path = str(tmp_path / "resumption.db")
    monkeypatch.setattr(agl, "DB_PATH", db_path)
    agl.init_db()

    assert agl.save_session_handle("room-alpha", "handle-abc123") is True
    assert agl.get_session_handle("room-alpha") == "handle-abc123"
    # room yang tidak ada → None (graceful)
    assert agl.get_session_handle("room-zeta") is None


def test_save_handle_overwrites_previous(monkeypatch, tmp_path):
    """Save baru meng-overwrite handle lama (upsert)."""
    db_path = str(tmp_path / "override.db")
    monkeypatch.setattr(agl, "DB_PATH", db_path)
    agl.init_db()

    agl.save_session_handle("room-beta", "handle-old")
    agl.save_session_handle("room-beta", "handle-new")
    assert agl.get_session_handle("room-beta") == "handle-new"
