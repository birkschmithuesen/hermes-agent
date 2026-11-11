"""Per-subscription wake-kind filter in build_wake_text().

Measured 2026-09-16 on the live board: 20 of 39 wake events (51 %) were
``review_requested`` / ``changes_requested`` — internal chain steps that ask
the human nothing. A subscription can now opt out of them without losing the
events that matter.
"""
from gateway import kanban_watchers_notifier as kwn


class _Ev:
    def __init__(self, kind, payload=None):
        self.kind = kind
        # Upstream's diagnostic_event() reads .payload on blocked-family events.
        self.payload = payload


def _notification(kinds, wake_kinds=None, delivery_mode="notify+wake"):
    sub = {
        "task_id": "t_measure", "platform": "telegram", "chat_id": "570261709",
        "thread_id": "1743586", "chat_type": "thread", "notifier_profile": "birk",
        "delivery_mode": delivery_mode,
    }
    if wake_kinds is not None:
        sub["wake_kinds"] = wake_kinds
    d = {"sub": sub, "task": None, "events": [_Ev(k) for k in kinds],
         "board": "hermes-dev", "cursor": 1, "old_cursor": 0}
    return kwn._KanbanNotification(object(), d, platform_cls=None, sub_fail_counts={})


def test_without_the_column_every_kind_still_wakes():
    n = _notification(["review_requested", "blocked"])
    n.build_wake_text()
    assert n.wake_kinds == {"review_requested", "blocked"}
    assert n.synth != ""


def test_filter_drops_the_internal_chain_kinds():
    n = _notification(
        ["review_requested", "changes_requested", "blocked"],
        wake_kinds="blocked,gave_up,crashed,timed_out,block_loop_detected",
    )
    n.build_wake_text()
    assert n.wake_kinds == {"blocked"}
    assert "handed off for review" not in n.synth


def test_empty_intersection_wakes_nobody():
    n = _notification(["review_requested"], wake_kinds="blocked")
    n.build_wake_text()
    assert n.wake_kinds == set()
    assert n.synth == ""


def test_unknown_kinds_in_the_column_are_ignored():
    n = _notification(["blocked"], wake_kinds=" bloecked , blocked ")
    n.build_wake_text()
    assert n.wake_kinds == {"blocked"}


def test_filter_is_inert_without_wake_mode():
    n = _notification(["blocked"], wake_kinds="blocked", delivery_mode="notify")
    n.build_wake_text()
    assert n.wake_kinds == set()
