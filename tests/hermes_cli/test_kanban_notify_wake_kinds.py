"""Per-subscription wake-kind filter: column, migration, inheritance.

NULL means "every _WAKE_KINDS entry", i.e. exactly the behaviour subscriptions
had before the column existed — there is deliberately NO backfill.
"""
import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

_LEGACY_SUBS_DDL = (
    "CREATE TABLE kanban_notify_subs ("
    " task_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL,"
    " thread_id TEXT NOT NULL DEFAULT '', user_id TEXT, user_id_alt TEXT,"
    " chat_type TEXT, notifier_profile TEXT,"
    " delivery_mode TEXT NOT NULL DEFAULT 'notify', delivery_metadata TEXT,"
    " created_at INTEGER NOT NULL, last_event_id INTEGER NOT NULL DEFAULT 0,"
    " last_ping_event_id INTEGER NOT NULL DEFAULT 0,"
    " PRIMARY KEY (task_id, platform, chat_id, thread_id))"
)


def _sub(conn, task_id, **kw):
    kbn.add_notify_sub(
        conn, task_id=task_id, platform="telegram", chat_id="570261709",
        thread_id="1743586", chat_type="thread", delivery_mode="notify+wake", **kw,
    )


def _row(conn, task_id):
    return next(s for s in kbn.list_notify_subs(conn, task_id))


def test_wake_kinds_defaults_to_null():
    conn = kbc.connect()
    task_id = kb.create_task(conn, title="no filter", assignee="worker")
    _sub(conn, task_id)
    assert _row(conn, task_id)["wake_kinds"] is None


def test_wake_kinds_roundtrip():
    conn = kbc.connect()
    task_id = kb.create_task(conn, title="filtered", assignee="worker")
    _sub(conn, task_id, wake_kinds="blocked,completed")
    assert _row(conn, task_id)["wake_kinds"] == "blocked,completed"


def test_none_leaves_existing_value_untouched():
    conn = kbc.connect()
    task_id = kb.create_task(conn, title="resubscribe", assignee="worker")
    _sub(conn, task_id, wake_kinds="blocked")
    _sub(conn, task_id)  # re-subscribe without the flag
    assert _row(conn, task_id)["wake_kinds"] == "blocked"


def test_explicit_value_is_last_write_wins():
    conn = kbc.connect()
    task_id = kb.create_task(conn, title="retune", assignee="worker")
    _sub(conn, task_id, wake_kinds="blocked")
    _sub(conn, task_id, wake_kinds="blocked,timed_out")
    assert _row(conn, task_id)["wake_kinds"] == "blocked,timed_out"


def test_child_task_inherits_wake_kinds():
    conn = kbc.connect()
    parent = kb.create_task(conn, title="parent", assignee="worker")
    _sub(conn, parent, wake_kinds="blocked")
    child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
    assert _row(conn, child)["wake_kinds"] == "blocked"


def test_legacy_db_gets_the_column_without_backfill(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(kb.SCHEMA_SQL)
    conn.execute("DROP TABLE kanban_notify_subs")
    conn.execute(_LEGACY_SUBS_DDL)
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id,"
        " created_at, delivery_mode) VALUES ('t_old','telegram','570261709','1743586',0,'notify+wake')"
    )
    conn.commit()

    kbc._migrate_add_optional_columns(conn)

    row = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = 't_old'").fetchone()
    assert "wake_kinds" in row.keys()
    # No backfill: the 62 live subscriptions must keep waking on every kind.
    assert row["wake_kinds"] is None


def test_rebuild_spec_carries_the_column():
    # A drift rebuild DROPs and recreates the table; a column missing from the
    # spec would vanish silently on the next connect().
    assert "wake_kinds" in kbc._REBUILD_SPECS["kanban_notify_subs"][0]


def test_wake_kinds_constant_is_shared_with_the_notifier():
    from gateway import kanban_watchers_notifier as kwn
    # One source of truth: the notifier must not carry its own copy.
    assert kwn._WAKE_KINDS is kbn.WAKE_KINDS
    assert "review_requested" in kbn.WAKE_KINDS
    assert "changes_requested" in kbn.WAKE_KINDS


def test_cli_accepts_wake_kinds_flag():
    import argparse

    from hermes_cli import kanban_parser
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kanban_parser.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args([
        "kanban", "notify-subscribe", "t_abc", "--platform", "telegram",
        "--chat-id", "570261709", "--thread-id", "1743586",
        "--wake-kinds", "blocked,timed_out",
    ])
    assert args.wake_kinds == "blocked,timed_out"
