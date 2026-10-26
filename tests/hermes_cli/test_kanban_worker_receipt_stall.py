"""The Kanban submitter keeps polling an owner whose receipt answers stall.

2026-10-26 00:58 (board kapsel, runs 1382/1392): the host swapped, the owner's event loop
stalled ~30 s, one ``prompt.receipt`` poll went unanswered past the 30 s RPC default, and the
TimeoutError left ``connect_gateway`` as "Gateway connection failed; no local fallback". The
submitter exited and its liveness watcher ended the already admitted, hour-long run. A receipt
read is idempotent: an unanswered poll is asked again on the same socket. Time is scaled 1000x.
"""
import asyncio
import json

import pytest

from hermes_cli import kanban_worker_client
from hermes_cli.gateway_client import GatewayClient, GatewayClientError


class _StallingOwner:
    """Admits at once, leaves the first ``prompt.receipt`` poll unanswered, answers the rest."""

    def __init__(self, unanswered=1, final='terminal'):
        self.inbox, self.sent = asyncio.Queue(), []
        self.unanswered, self.final = unanswered, final

    async def send(self, raw):
        req = json.loads(raw)
        self.sent.append(req)
        if req['method'] == 'kanban.run':
            await self._answer(req, {'session_id': 's1', 'receipt': {'status': 'started', 'admission_id': 'a1'}})
        elif req['method'] == 'prompt.receipt':
            if self.unanswered > 0:
                self.unanswered -= 1  # the stalled loop never sends this answer
                return
            await self._answer(req, {'status': self.final, 'admission_id': 'a1'})

    async def _answer(self, req, result):
        await self.inbox.put(json.dumps({'id': req['id'], 'result': result}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.inbox.get()


def _scale(monkeypatch):
    real_wait_for = asyncio.wait_for
    monkeypatch.setattr(asyncio, 'wait_for', lambda fut, timeout: real_wait_for(
        fut, None if timeout is None else timeout / 1000))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, 'sleep', lambda s, *a: real_sleep(s / 1000, *a))
    return real_wait_for


@pytest.mark.asyncio
async def test_an_unanswered_receipt_poll_is_asked_again_not_failed(monkeypatch, tmp_path):
    real_wait_for = _scale(monkeypatch)
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, 'worker_result', lambda db, params: {'exit_code': 0, 'last_output': 'done'})
    owner = _StallingOwner(unanswered=2)
    params = {'board': 'b', 'task_id': 't1', 'run_id': 1, 'claim_lock': 'lock'}
    async with GatewayClient(owner) as client:
        code = await real_wait_for(kanban_worker_client._submit_claim(client, params, str(tmp_path / 'kanban.db')), 10)
    assert code == 0
    methods = [r['method'] for r in owner.sent]
    # The claim is submitted once; only the idempotent receipt read is repeated.
    assert methods.count('kanban.run') == 1 and methods.count('prompt.receipt') == 3


@pytest.mark.asyncio
async def test_a_receipt_stall_past_the_budget_still_fails(monkeypatch, tmp_path):
    real_wait_for = _scale(monkeypatch)
    monkeypatch.setattr(kanban_worker_client, 'RECEIPT_STALL_BUDGET_S', 0.0)
    owner = _StallingOwner(unanswered=10**6)
    params = {'board': 'b', 'task_id': 't1', 'run_id': 1, 'claim_lock': 'lock'}
    async with GatewayClient(owner) as client:
        with pytest.raises(TimeoutError):
            await real_wait_for(kanban_worker_client._submit_claim(client, params, str(tmp_path / 'kanban.db')), 10)


@pytest.mark.asyncio
async def test_an_unknown_receipt_is_still_reported_unknown(monkeypatch, tmp_path):
    real_wait_for = _scale(monkeypatch)
    owner = _StallingOwner(unanswered=1, final='unknown')
    params = {'board': 'b', 'task_id': 't1', 'run_id': 1, 'claim_lock': 'lock'}
    async with GatewayClient(owner) as client:
        with pytest.raises(GatewayClientError, match='execution unknown'):
            await real_wait_for(kanban_worker_client._submit_claim(client, params, str(tmp_path / 'kanban.db')), 10)
