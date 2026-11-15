"""A managed worker never gives up on an owner RPC before the owner's own write patience ends.

The worker used to wait a fixed 20 s for a ``worker.persist`` receipt while the owner may wait
``SessionDB._TRANSCRIPT_WRITE_PATIENCE_S`` (60 s) on its state.db write lock before it answers;
under host load the worker died with ``worker_receipt_timeout`` on writes the owner then committed.
Imports stay inside the tests so a missing symbol fails each test, not the collection.
"""


def test_the_worker_never_gives_up_before_the_owner():
    from hermes_state import SessionDB
    from agent.runtime_session_store import WORKER_RECEIPT_MARGIN_S, worker_receipt_budget
    patience = SessionDB._TRANSCRIPT_WRITE_PATIENCE_S
    assert worker_receipt_budget() == patience + WORKER_RECEIPT_MARGIN_S
    assert worker_receipt_budget() >= patience + 1


def test_the_budget_follows_the_owners_write_patience(monkeypatch):
    from hermes_state import SessionDB
    from agent.runtime_session_store import WORKER_RECEIPT_MARGIN_S, worker_receipt_budget
    monkeypatch.setattr(SessionDB, '_TRANSCRIPT_WRITE_PATIENCE_S', 300.0)
    assert worker_receipt_budget() == 300.0 + WORKER_RECEIPT_MARGIN_S
