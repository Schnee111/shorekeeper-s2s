import concurrent.futures
import sqlite3

import pytest

from task_store_client import TaskStoreClient, TaskStoreError


def test_atomic_task_creation_with_outbox(tmp_path):
    db_file = str(tmp_path / "tasks.db")
    store = TaskStoreClient(db_file)

    task_id = "task_test_01"
    res = store.create_task(
        task_id=task_id,
        user_intent="Fix memory leak",
        session_room="test-room",
        lane="debug",
    )
    assert res["task_id"] == task_id
    assert res["status"] == "queued"

    # Verify task in tasks table
    task = store.get_task(task_id)
    assert task is not None
    assert task["user_intent"] == "Fix memory leak"
    assert task["status"] == "queued"

    # Verify event in task_outbox
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        outbox = conn.execute(
            "SELECT * FROM task_outbox WHERE task_id = ?", (task_id,)
        ).fetchone()
        assert outbox is not None
        assert outbox["event_type"] == "task.accepted"
        assert outbox["published"] == 0


def test_task_id_regex_validation(tmp_path):
    store = TaskStoreClient(str(tmp_path / "tasks.db"))
    with pytest.raises(TaskStoreError, match="INVALID_TASK_ID"):
        store.create_task(task_id="../evil/traversal", user_intent="do harm")


def test_concurrent_wal_writes(tmp_path):
    db_file = str(tmp_path / "tasks.db")
    store = TaskStoreClient(db_file)

    def worker(i):
        tid = f"task_conc_{i}"
        store.create_task(tid, f"intent {i}")
        return tid

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(worker, range(20)))

    assert len(results) == 20
    assert len(store.list_recent_tasks(limit=50)) == 20
