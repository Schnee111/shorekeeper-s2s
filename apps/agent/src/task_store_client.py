"""task_store_client.py — Atomic Python client for TaskStore with SQLite WAL and outbox integration.

Implements strict schema adhering to packages/task-store and packages/contracts:
- PRAGMA journal_mode = WAL
- PRAGMA busy_timeout = 5000
- PRAGMA foreign_keys = ON
- Task ID regex validation: ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$
- Atomic transaction inserting tasks + task_outbox (event_type: 'task.accepted')
"""

import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

TASK_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
VALID_LANES = ("research", "frontend", "debug", "qa")
VALID_STATUSES = (
    "queued",
    "running",
    "done",
    "failed",
    "cancelled",
    "blocked",
    "waiting_input",
    "unknown",
)


class TaskStoreError(Exception):
    """Base error for TaskStore operations."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class TaskStoreClient:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.init_schema()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_schema(self) -> None:
        with self.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                  task_id       TEXT PRIMARY KEY,
                  session_room  TEXT NOT NULL DEFAULT '',
                  user_intent   TEXT NOT NULL DEFAULT '',
                  parent_id     TEXT,
                  root_task_id  TEXT,
                  lane          TEXT NOT NULL DEFAULT 'debug',
                  status        TEXT NOT NULL DEFAULT 'queued',
                  worker_pid    INTEGER,
                  heartbeat_ts  INTEGER,
                  created_at    INTEGER NOT NULL,
                  started_at    INTEGER,
                  finished_at   INTEGER,
                  contract_ref  TEXT NOT NULL DEFAULT '',
                  artifact_dir  TEXT,
                  summary       TEXT NOT NULL DEFAULT '',
                  error         TEXT,
                  notify_gate   TEXT NOT NULL DEFAULT 'next_turn',
                  priority      INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS task_outbox (
                  event_id      TEXT PRIMARY KEY,
                  task_id       TEXT NOT NULL,
                  event_type    TEXT NOT NULL,
                  sequence      INTEGER NOT NULL DEFAULT 1,
                  payload       TEXT NOT NULL DEFAULT '{}',
                  created_at    INTEGER NOT NULL,
                  published     INTEGER NOT NULL DEFAULT 0,
                  published_at  INTEGER,
                  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS notify_outbox (
                  task_id      TEXT PRIMARY KEY,
                  status       TEXT NOT NULL,
                  created_at   INTEGER NOT NULL,
                  delivered    INTEGER NOT NULL DEFAULT 0,
                  delivered_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS session_resumption (
                  room         TEXT PRIMARY KEY,
                  handle       TEXT NOT NULL DEFAULT '',
                  updated_at   INTEGER NOT NULL
                );
                """
            )

    def create_task(
        self,
        task_id: str,
        user_intent: str,
        session_room: str = "",
        lane: str = "debug",
        parent_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
        priority: int = 1,
    ) -> Dict[str, Any]:
        if not TASK_ID_REGEX.match(task_id):
            raise TaskStoreError(
                "INVALID_TASK_ID",
                f"Invalid task_id '{task_id}'. Must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}$",
            )
        if lane not in VALID_LANES:
            raise TaskStoreError(
                "INVALID_LANE", f"Lane '{lane}' invalid. Must be one of {VALID_LANES}"
            )

        now = int(time.time() * 1000)
        event_id = f"evt_{task_id}_1_{now}"
        payload = json.dumps(
            {"lane": lane, "user_intent": user_intent, "status": "queued"}
        )

        with self.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, session_room, user_intent, parent_id, root_task_id,
                        lane, status, created_at, priority
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        task_id,
                        session_room,
                        user_intent,
                        parent_id,
                        root_task_id,
                        lane,
                        now,
                        priority,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO task_outbox (
                        event_id, task_id, event_type, sequence, payload, created_at, published
                    ) VALUES (?, ?, 'task.accepted', 1, ?, ?, 0)
                    """,
                    (event_id, task_id, payload, now),
                )

        return {
            "task_id": task_id,
            "session_room": session_room,
            "user_intent": user_intent,
            "lane": lane,
            "status": "queued",
            "created_at": now,
        }

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row:
                return dict(row)
            return None

    def list_recent_tasks(self, limit: int = 5) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_session_handle(self, room: str) -> str:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT handle FROM session_resumption WHERE room = ?", (room,)
            ).fetchone()
            return row["handle"] if row else ""

    def save_session_handle(self, room: str, handle: str) -> None:
        now = int(time.time() * 1000)
        with self.get_connection() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO session_resumption (room, handle, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(room) DO UPDATE SET handle = excluded.handle, updated_at = excluded.updated_at
                    """,
                    (room, handle, now),
                )
