"""Unit tests Sprint C: outbox claim atomik + interrupt + coalesce + health check.

Tidak ada network/LiveKit call — mock session.say (SpeechHandle-like) dan SQLite tmp.
"""

import sqlite3
from unittest.mock import patch

import pytest

import agent_gemini_live as agl


class FakeSpeechHandle:
    def __init__(self, interrupted=False):
        self.interrupted = interrupted
        self.waited = False

    async def wait_for_playout(self):
        self.waited = True

    async def wait_if_not_interrupted(self):
        self.waited = True


class FakeSession:
    """Mock AgentSession: say() configurable (sukses / gagal / interrupted)."""

    def __init__(self, fail=False, interrupted=False):
        self.fail = fail
        self.interrupted = interrupted
        self.said = []

    async def say(self, text):
        if self.fail:
            raise RuntimeError("say failed")
        self.said.append(text)
        return FakeSpeechHandle(interrupted=self.interrupted)


def _seed_db(db_path: str, n_tasks: int, room: str = "room-1"):
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY, session_room TEXT NOT NULL DEFAULT '',
              user_intent TEXT NOT NULL DEFAULT '', parent_id TEXT,
              lane TEXT NOT NULL DEFAULT 'debug', status TEXT NOT NULL DEFAULT 'queued',
              worker_pid INTEGER, heartbeat_ts INTEGER, created_at INTEGER NOT NULL,
              started_at INTEGER, finished_at INTEGER, contract_ref TEXT NOT NULL DEFAULT '',
              artifact_dir TEXT, summary TEXT NOT NULL DEFAULT '', error TEXT,
              notify_gate TEXT NOT NULL DEFAULT 'next_turn', priority INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS notify_outbox (
              task_id TEXT PRIMARY KEY, status TEXT NOT NULL,
              created_at INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
              delivered_at INTEGER
            );
            """
        )
        for i in range(n_tasks):
            conn.execute(
                "INSERT INTO tasks (task_id, session_room, user_intent, status, created_at) "
                "VALUES (?, ?, ?, 'done', ?)",
                (f"task_{i}", room, f"perbaiki bug {i}", 1000 + i),
            )
            conn.execute(
                "INSERT INTO notify_outbox (task_id, status, created_at, delivered) "
                "VALUES (?, 'done', ?, 0)",
                (f"task_{i}", 1000 + i),
            )


def _delivered(db_path: str, tid: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT delivered FROM notify_outbox WHERE task_id = ?", (tid,)
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# C.1 — claim atomik + rollback saat say() gagal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_success_marks_delivered(monkeypatch, tmp_path):
    db = str(tmp_path / "c1.db")
    _seed_db(db, 1)
    sess = FakeSession()
    n = await agl.deliver_notifications(sess, "room-1", db_path=db)
    assert n == 1
    assert _delivered(db, "task_0") == 1
    assert len(sess.said) == 1


@pytest.mark.asyncio
async def test_deliver_say_fail_keeps_pending(monkeypatch, tmp_path):
    """Simulasi say() gagal → notifikasi tetap pending (delivered=0), tidak hilang."""
    db = str(tmp_path / "c1b.db")
    _seed_db(db, 1)
    sess = FakeSession(fail=True)
    n = await agl.deliver_notifications(sess, "room-1", db_path=db)
    assert n == 0
    assert _delivered(db, "task_0") == 0  # rollback: tetap pending


# ---------------------------------------------------------------------------
# C.2 — hormati interupsi
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_interrupted_rolls_back(monkeypatch, tmp_path):
    """Ucapan di-interupsi → rollback delivered=0 → ditawarkan ulang di poll berikutnya."""
    db = str(tmp_path / "c2.db")
    _seed_db(db, 1)
    sess = FakeSession(interrupted=True)
    n = await agl.deliver_notifications(sess, "room-1", db_path=db)
    assert n == 0
    assert _delivered(db, "task_0") == 0  # rollback karena interrupted


# ---------------------------------------------------------------------------
# C.3 — coalesce multi-task jadi SATU ucapan (maks 5, urut created_at ASC)
# ---------------------------------------------------------------------------


def test_coalesce_single():
    rows = [{"task_id": "t1", "user_intent": "fix login", "summary": "ok"}]
    out = agl.coalesce_notifications(rows)
    assert "fix login" in out
    assert "Schnee" in out


def test_coalesce_multiple_natural_word():
    rows = [
        {"task_id": f"t{i}", "user_intent": f"task {i}", "summary": ""}
        for i in range(3)
    ]
    out = agl.coalesce_notifications(rows)
    assert "tiga tugas" in out


def test_coalesce_max_five_items():
    rows = [
        {"task_id": f"t{i}", "user_intent": f"task {i}", "summary": ""}
        for i in range(7)
    ]
    # deliver_notifications membatasi via LIMIT; coalesce sendiri maks 5 input
    out = agl.coalesce_notifications(rows[: agl.COALESCE_MAX])
    assert "lima tugas" in out


@pytest.mark.asyncio
async def test_deliver_coalesces_to_one_utterance(monkeypatch, tmp_path):
    """3 baris ready dalam satu jendela poll → SATU ucapan gabungan."""
    db = str(tmp_path / "c3.db")
    _seed_db(db, 3)
    sess = FakeSession()
    n = await agl.deliver_notifications(sess, "room-1", db_path=db)
    assert n == 3
    assert len(sess.said) == 1  # SATU ucapan gabungan
    assert "tiga tugas di latar belakang telah selesai" in sess.said[0]
    for i in range(3):
        assert _delivered(db, f"task_{i}") == 1


@pytest.mark.asyncio
async def test_deliver_limits_to_five(tmp_path):
    db = str(tmp_path / "c3b.db")
    _seed_db(db, 7)
    sess = FakeSession()
    n = await agl.deliver_notifications(sess, "room-1", db_path=db)
    assert n == 5  # maks 5 item per jendela poll
    assert _delivered(db, "task_5") == 0  # sisanya tetap pending


# ---------------------------------------------------------------------------
# C.4 — health check startup (non fail-fast)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_health_check_all_down_no_crash(monkeypatch):
    """Jika dependency down → log warning, return dict, JANGAN raise."""

    class DownSession:
        def get(self, url, **kwargs):
            raise ConnectionError("down")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.delenv("MEMPALACE_MCP_HTTP_ENDPOINT", raising=False)
    with patch.object(agl.aiohttp, "ClientSession", return_value=DownSession()):
        health = await agl.startup_health_check()
    assert health == {"searxng": False, "mempalace": False}  # tidak raise
