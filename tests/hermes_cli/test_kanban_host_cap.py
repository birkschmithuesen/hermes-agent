"""Host-level concurrency accounting + review-lane fairness (OOF-30 review).

Three gaps found in review of the original memory-guard PR:

1. The standalone daemon path (``hermes kanban daemon --force`` /
   :func:`hermes_cli.kanban_db_dispatch.run_daemon`) never resolved
   ``kanban.max_in_progress`` at all — the one shipped entry point that
   could still fan out an entire backlog in a single tick.
2. ``max_in_progress`` was enforced per-board while the gateway dispatcher
   ticks every active board — N boards multiplied the host budget by N.
3. The ready loop consumed the entire shared spawn budget before the
   review loop ran, so a sustained ready backlog starved autonomous
   reviews indefinitely.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


# ---------------------------------------------------------------------------
# 1. Standalone daemon resolves max_in_progress (P1a)
# ---------------------------------------------------------------------------


def test_run_daemon_resolves_and_passes_max_in_progress(
    kanban_home, monkeypatch,
):
    """The daemon tick must pass a resolved cap into dispatch_once.

    Regression guard for the OOF-30 review finding: ``run_daemon`` only
    forwarded ``max_spawn`` — with no explicit ``--max`` (the shipped
    systemd shape) nothing capped the tick even though the gateway and
    ``hermes kanban dispatch`` paths both resolved the memory-derived
    default.
    """
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    # No explicit config → the derived default must flow through.
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: None)
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 3)

    def on_tick(res):
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured.get("max_in_progress") == 3




def test_configured_max_in_progress_parsing(monkeypatch):
    import hermes_cli.config as cfgmod

    cases = [
        ({"kanban": {"max_in_progress": 4}}, 4),
        ({"kanban": {"max_in_progress": "5"}}, 5),
        ({"kanban": {"max_in_progress": 0}}, None),
        ({"kanban": {"max_in_progress": -2}}, None),
        ({"kanban": {"max_in_progress": "lots"}}, None),
        ({"kanban": {}}, None),
        ({}, None),
    ]
    for config, expected in cases:
        monkeypatch.setattr(
            cfgmod, "load_config_readonly", lambda c=config: c
        )
        assert kbd.configured_max_in_progress() == expected, config


# ---------------------------------------------------------------------------
# 2. max_in_progress counts running work on ALL boards (P1b)
# ---------------------------------------------------------------------------


def test_max_in_progress_counts_other_boards(
    kanban_home, all_assignees_spawnable,
):
    """Workers running on another board consume the same host budget."""
    kb.create_board("second")

    # Two workers already running on the second board.
    with kbc.connect(board="second") as conn:
        for title in ("busy-1", "busy-2"):
            tid = kb.create_task(conn, title=title, assignee="alice")
            assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Host budget (2) already consumed by the second board → nothing spawns.
    assert not spawns
    assert not res.spawned


def test_max_in_progress_partial_budget_across_boards(
    kanban_home, all_assignees_spawnable,
):
    kb.create_board("second")

    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # 1 running elsewhere + budget 2 → exactly one new spawn here.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


def test_count_running_tasks_other_boards_fails_open(
    kanban_home, monkeypatch,
):
    """A broken board enumeration must not brick dispatch (returns 0)."""
    monkeypatch.setattr(
        kb, "list_boards",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert kbd.count_running_tasks_other_boards() == 0


def test_max_spawn_stays_per_board(kanban_home, all_assignees_spawnable):
    """``max_spawn`` keeps its historical per-board semantics."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=1,
        )

    # The other board's worker does NOT count against max_spawn.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


# ---------------------------------------------------------------------------
# 3. Review lane cannot be starved by a sustained ready backlog (P2)
# ---------------------------------------------------------------------------


def _park_in_review(conn: sqlite3.Connection, title: str, assignee: str) -> str:
    tid = kb.create_task(conn, title=title, assignee=assignee)
    _set_task_status(conn, tid, "review")
    return tid


def test_review_lane_gets_reserved_slot_under_ready_backlog(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2", "ready-3"):
            kb.create_task(conn, title=title, assignee="alice")
        review_id = _park_in_review(conn, "review-me", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    spawned_ids = [s[0] for s in res.spawned]
    # Budget 2: one ready + the reserved review slot — never 2×ready.
    assert len(spawned_ids) == 2
    assert review_id in spawned_ids


def _guard_review_row(conn: sqlite3.Connection, review_id: str) -> dict:
    """Latest run ``rate_limited`` → ``check_respawn_guard`` returns a cooldown."""
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, "
            "started_at, ended_at) VALUES (?, 'reviewer', 'rate_limited', "
            "'rate_limited', ?, ?)",
            (review_id, now, now),
        )
    assert kbd.check_respawn_guard(conn, review_id, lane="review") == "rate_limit_cooldown"
    return {"max_in_progress": 1}


def _cap_review_row(conn: sqlite3.Connection, review_id: str) -> dict:
    """``reviewer`` already has one running worker → the review row is per-profile capped."""
    busy_id = kb.create_task(conn, title="busy", assignee="reviewer")
    assert kb.claim_task(conn, busy_id) is not None
    return {"max_in_progress": 2, "max_in_progress_per_profile": 1}


@pytest.mark.parametrize("make_unspawnable", [_guard_review_row, _cap_review_row])
def test_unspawnable_review_does_not_reserve_the_only_ready_slot(
    kanban_home, all_assignees_spawnable, monkeypatch, make_unspawnable,
):
    """A review card the review loop would refuse this tick (respawn guard,
    per-profile cap) must not consume the fairness reservation — otherwise the
    ready lane starves every tick while the reserved slot goes unused."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        ready_id = kb.create_task(conn, title="ready-now", assignee="alice")
        review_id = _park_in_review(conn, "review-unspawnable", "reviewer")
        caps = make_unspawnable(conn, review_id)
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), **caps)

    assert [task_id for task_id, *_ in res.spawned] == [ready_id]


def test_unguarded_review_reserves_the_only_ready_slot(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A dispatchable review card still receives the single shared slot."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="ready-now", assignee="alice")
        review_id = _park_in_review(conn, "review-now", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1,
        )

    assert [task_id for task_id, *_ in res.spawned] == [review_id]


def test_review_reservation_released_when_no_review_work(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2", "ready-3"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # No review work → ready lane keeps the full budget.
    assert len(res.spawned) == 2


def test_nonspawnable_review_does_not_tax_ready_budget(
    kanban_home, monkeypatch,
):
    """Review tasks parked for humans (no real profile) release the slot."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    # Only 'alice' is a real profile; the review assignee is a human lane.
    monkeypatch.setattr(
        profmod, "profile_exists", lambda name: name == "alice"
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2"):
            kb.create_task(conn, title=title, assignee="alice")
        _park_in_review(conn, "human-review", "some-human")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Human-lane review is not spawnable → no reservation, ready gets both.
    assert len(res.spawned) == 2


def test_review_budget_still_bounded_by_shared_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The reservation caps the ready lane; it grants review no extra slots."""
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="ready-1", assignee="alice")
        for i in range(3):
            _park_in_review(conn, f"review-{i}", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Budget 2 total across both lanes, reservation notwithstanding.
    assert len(res.spawned) == 2


# ---------------------------------------------------------------------------
# kanban.max_in_progress_per_board: one board cannot take every host slot
# ---------------------------------------------------------------------------


def _per_board_caps(monkeypatch, caps):
    monkeypatch.setattr(kbd, "configured_max_in_progress_per_board", lambda: dict(caps))


def test_per_board_cap_leaves_host_slots_for_other_boards(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Boards tick in name order; without a per-board cap the first board with a
    backlog fills the whole host budget. With a cap it stops at its share."""
    _per_board_caps(monkeypatch, {kb.DEFAULT_BOARD: 2})
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c", "d"):
            kb.create_task(conn, title=title, assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=4)
    assert len(spawns) == 2


def test_per_board_cap_counts_only_its_own_running(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    _per_board_caps(monkeypatch, {kb.DEFAULT_BOARD: 2})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None
        kb.create_task(conn, title="x", assignee="alice")
        kb.create_task(conn, title="y", assignee="alice")
        spawns: list = []
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=4)
    assert len(spawns) == 1


def test_per_board_cap_never_exceeds_host_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    _per_board_caps(monkeypatch, {kb.DEFAULT_BOARD: 3})
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        for title in ("s1", "s2", "s3"):
            tid = kb.create_task(conn, title=title, assignee="alice")
            assert kb.claim_task(conn, tid) is not None
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=4)
    assert len(spawns) == 1


def test_board_without_entry_is_uncapped_below_host(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    _per_board_caps(monkeypatch, {"other": 1})
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=4)
    assert len(spawns) == 3


def test_configured_max_in_progress_per_board_parsing(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"kanban": {
        "max_in_progress_per_board": {"kapsel": 2, "cothinker": "2", "bad": "x", "zero": 0}}})
    assert kbd.configured_max_in_progress_per_board() == {"kapsel": 2, "cothinker": 2}
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"kanban": {"max_in_progress_per_board": 3}})
    assert kbd.configured_max_in_progress_per_board() == {}


def test_configured_max_in_progress_per_board_ignores_pool_entries(monkeypatch):
    """A mapping-valued (pool) entry must not confuse the plain int parser —
    it silently drops it, same as any other non-int value."""
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"kanban": {
        "max_in_progress_per_board": {
            "kapsel": 2, "grundlast": {"cap": 4, "boards": ["a", "b"]},
        }}})
    assert kbd.configured_max_in_progress_per_board() == {"kapsel": 2}


# ---------------------------------------------------------------------------
# kanban.max_in_progress_per_board (pool form, card t_5d01c417): a mapping
# entry shares one cap across several boards and/or assignee glob patterns,
# with cross-board priority ordering.
# ---------------------------------------------------------------------------


def _pool_caps(monkeypatch, pools):
    monkeypatch.setattr(kbd, "configured_max_in_progress_pools", lambda: dict(pools))


def test_configured_max_in_progress_pools_parsing(monkeypatch):
    import hermes_cli.config as cfg
    valid = {"cap": 4, "boards": ["wissen", "marketing", "produkt"], "assignees": ["*-g"]}
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"kanban": {
        "max_in_progress_per_board": {
            "kapsel": 3,
            "grundlast": valid,
            "missing_cap": {"boards": ["x"]},
            "zero_cap": {"cap": 0, "boards": ["x"]},
            "string_cap": {"cap": "4", "boards": ["x"]},
            "bad_boards": {"cap": 2, "boards": "notalist"},
            "bad_assignees": {"cap": 2, "boards": ["x"], "assignees": [1, 2]},
        }}})
    pools = kbd.configured_max_in_progress_pools()
    assert set(pools) == {"grundlast"}
    pool = pools["grundlast"]
    assert pool.cap == 4
    assert pool.boards == ("wissen", "marketing", "produkt")
    assert pool.assignees == ("*-g",)

    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"kanban": {"max_in_progress_per_board": 3}})
    assert kbd.configured_max_in_progress_pools() == {}


def test_pool_cap_counts_running_across_member_boards(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Pool running count sums running tasks across every board listed in `boards`."""
    kb.create_board("second")
    pool = kbd.PoolCap(name="pool", cap=2, boards=(kb.DEFAULT_BOARD, "second"), assignees=())
    _pool_caps(monkeypatch, {"pool": pool})
    with kbc.connect(board="second") as other_conn:
        tid = kb.create_task(other_conn, title="busy", assignee="alice")
        assert kb.claim_task(other_conn, tid) is not None
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b"):
            kb.create_task(conn, title=title, assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    # pool running = 1 (the "second" board) → only 1 of 2 slots left under cap 2.
    assert len(spawns) == 1


def test_pool_cap_counts_running_matching_assignee_pattern(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A running task on a board NOT listed in `boards` still counts toward the
    pool when its assignee matches an `assignees` glob pattern."""
    kb.create_board("other")
    pool = kbd.PoolCap(name="pool", cap=1, boards=(kb.DEFAULT_BOARD,), assignees=("*-g",))
    _pool_caps(monkeypatch, {"pool": pool})
    with kbc.connect(board="other") as other_conn:
        tid = kb.create_task(other_conn, title="busy", assignee="backend-g")
        assert kb.claim_task(other_conn, tid) is not None
    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert spawns == []


def test_pool_cap_counts_running_task_once_when_matching_both_board_and_assignee(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A running task on a pool-member board whose assignee ALSO matches the
    pool's assignee pattern counts once, not twice (req 2 dedup)."""
    pool = kbd.PoolCap(name="pool", cap=2, boards=(kb.DEFAULT_BOARD,), assignees=("*-g",))
    _pool_caps(monkeypatch, {"pool": pool})
    spawns: list = []
    with kbc.connect() as conn:
        running_id = kb.create_task(conn, title="busy", assignee="backend-g")
        assert kb.claim_task(conn, running_id) is not None
        kb.create_task(conn, title="a", assignee="alice")
        kb.create_task(conn, title="b", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    # Running count must be 1 (not 2): cap 2 leaves room for exactly one spawn.
    assert len(spawns) == 1


def test_pool_cap_blocks_ready_task_on_unlisted_board_via_assignee_match(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A ready task's own board need not be listed in `boards` — if its
    assignee matches an `assignees` pattern, the pool cap still applies to it
    (req 1 membership also gates the task's own spawn, not just counting)."""
    pool = kbd.PoolCap(name="pool", cap=1, boards=(kb.DEFAULT_BOARD,), assignees=("*-g",))
    _pool_caps(monkeypatch, {"pool": pool})
    with kbc.connect() as default_conn:
        busy_id = kb.create_task(default_conn, title="busy", assignee="alice")
        assert kb.claim_task(default_conn, busy_id) is not None
    kb.create_board("misc")
    spawns: list = []
    with kbc.connect(board="misc") as misc_conn:
        kb.create_task(misc_conn, title="a", assignee="backend-g")
        kbd.dispatch_once(misc_conn, spawn_fn=_fake_spawn_factory(spawns), board="misc")
    assert spawns == []


def test_pool_cap_blocks_once_at_cap(kanban_home, all_assignees_spawnable, monkeypatch):
    pool = kbd.PoolCap(name="pool", cap=1, boards=(kb.DEFAULT_BOARD,), assignees=())
    _pool_caps(monkeypatch, {"pool": pool})
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b"):
            kb.create_task(conn, title=title, assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert len(spawns) == 1


def test_pool_cap_and_board_int_cap_both_apply(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A board with both its own int cap AND pool membership must satisfy BOTH;
    here the (tighter) pool cap is the binding constraint."""
    _per_board_caps(monkeypatch, {kb.DEFAULT_BOARD: 5})
    pool = kbd.PoolCap(name="pool", cap=1, boards=(kb.DEFAULT_BOARD,), assignees=())
    _pool_caps(monkeypatch, {"pool": pool})
    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b"):
            kb.create_task(conn, title=title, assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=10)
    assert len(spawns) == 1


def test_pool_priority_defers_to_higher_priority_on_other_board(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    kb.create_board("alpha")
    kb.create_board("beta")
    pool = kbd.PoolCap(name="pool", cap=5, boards=("alpha", "beta"), assignees=())
    _pool_caps(monkeypatch, {"pool": pool})
    with kbc.connect(board="beta") as beta_conn:
        kb.create_task(beta_conn, title="important", assignee="alice", priority=10)
    alpha_spawns: list = []
    with kbc.connect(board="alpha") as alpha_conn:
        kb.create_task(alpha_conn, title="low", assignee="alice", priority=0)
        kbd.dispatch_once(alpha_conn, spawn_fn=_fake_spawn_factory(alpha_spawns), board="alpha")
    # alpha's lower-priority task defers to beta's higher-priority pool task.
    assert alpha_spawns == []
    beta_spawns: list = []
    with kbc.connect(board="beta") as beta_conn2:
        kbd.dispatch_once(beta_conn2, spawn_fn=_fake_spawn_factory(beta_spawns), board="beta")
    assert len(beta_spawns) == 1


def test_pool_priority_tie_break_older_created_at_wins(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    kb.create_board("alpha")
    kb.create_board("beta")
    pool = kbd.PoolCap(name="pool", cap=5, boards=("alpha", "beta"), assignees=())
    _pool_caps(monkeypatch, {"pool": pool})
    with kbc.connect(board="alpha") as alpha_conn:
        old_id = kb.create_task(alpha_conn, title="old", assignee="alice", priority=5)
        alpha_conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (1000, old_id))
    with kbc.connect(board="beta") as beta_conn:
        new_id = kb.create_task(beta_conn, title="new", assignee="alice", priority=5)
        beta_conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (2000, new_id))
    # Same priority: beta's newer task must defer to alpha's older one.
    beta_spawns: list = []
    with kbc.connect(board="beta") as beta_conn2:
        kbd.dispatch_once(beta_conn2, spawn_fn=_fake_spawn_factory(beta_spawns), board="beta")
    assert beta_spawns == []
    # alpha's older task is not blocked by anything.
    alpha_spawns: list = []
    with kbc.connect(board="alpha") as alpha_conn2:
        kbd.dispatch_once(alpha_conn2, spawn_fn=_fake_spawn_factory(alpha_spawns), board="alpha")
    assert len(alpha_spawns) == 1


def test_pool_priority_ignores_unspawnable_competing_task(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A higher-priority ready task on another pool board that cannot actually
    spawn right now (its assignee is already at max_in_progress_per_profile)
    must not block this board — else both boards could stall forever."""
    kb.create_board("alpha")
    kb.create_board("beta")
    pool = kbd.PoolCap(name="pool", cap=5, boards=("alpha", "beta"), assignees=())
    _pool_caps(monkeypatch, {"pool": pool})
    with kbc.connect(board="beta") as beta_conn:
        running_id = kb.create_task(beta_conn, title="busy", assignee="alice")
        assert kb.claim_task(beta_conn, running_id) is not None
        kb.create_task(beta_conn, title="important", assignee="alice", priority=10)
    spawns: list = []
    with kbc.connect(board="alpha") as alpha_conn:
        kb.create_task(alpha_conn, title="low", assignee="alice", priority=0)
        kbd.dispatch_once(
            alpha_conn, spawn_fn=_fake_spawn_factory(spawns), board="alpha",
            max_in_progress_per_profile=1,
        )
    assert len(spawns) == 1


def test_pool_counting_fails_open_when_other_board_db_unreadable(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A pool member board whose DB can't be read must not abort the tick or
    block spawning — it contributes 0, same as 'no competing task'."""
    kb.create_board("ghost")
    pool = kbd.PoolCap(name="pool", cap=1, boards=(kb.DEFAULT_BOARD, "ghost"), assignees=())
    _pool_caps(monkeypatch, {"pool": pool})
    real_connect = kbc.connect

    def flaky_connect(*args, **kwargs):
        if kwargs.get("board") == "ghost":
            raise RuntimeError("boom")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kbc, "connect", flaky_connect)
    spawns: list = []
    with real_connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert len(spawns) == 1


def test_pool_priority_defer_fails_open_when_other_board_db_unreadable(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    kb.create_board("ghost")
    pool = kbd.PoolCap(name="pool", cap=5, boards=(kb.DEFAULT_BOARD, "ghost"), assignees=())
    _pool_caps(monkeypatch, {"pool": pool})
    real_connect = kbc.connect

    def flaky_connect(*args, **kwargs):
        if kwargs.get("board") == "ghost":
            raise RuntimeError("boom")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kbc, "connect", flaky_connect)
    spawns: list = []
    with real_connect() as conn:
        kb.create_task(conn, title="a", assignee="alice", priority=0)
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert len(spawns) == 1
