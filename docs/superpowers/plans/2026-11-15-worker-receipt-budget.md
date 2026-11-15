# Worker Receipt Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A managed Kanban worker waits on every owner RPC step (connect and receipt) for at least as long as the owner itself may take to answer. This replaces the fixed 20 s / 5 s budgets that killed 28 runs on 2026-11-15 with `worker_receipt_timeout`.

**Architecture:** One function, `worker_receipt_budget()` in `agent/runtime_session_store.py`, returns `SessionDB._TRANSCRIPT_WRITE_PATIENCE_S + WORKER_RECEIPT_MARGIN_S` (120.0 s today). `WorkerRPC.__call__` and every step of `WorkerRPC._connection` read it. `hermes_cli.gateway_client._session_ticket` gains a keyword-only `timeout` that it passes through to `private_control_query`, with a default of 5 so that its other callers do not change.

**Tech Stack:** Python 3, `websockets.sync`, pytest through `scripts/run_tests.sh`.

**Spec (binding, D1-D6):** `docs/superpowers/specs/2026-11-15-worker-receipt-budget-design.md`. Diagnosis and decisions are final. Do not re-open them, and do not re-run the probes.

## Global Constraints

- No change to the semantics of `WorkerRPC.__call__`: **never resend after a send** (the only redial-and-resend is the existing `ConnectionClosed`-from-`send` branch), **sticky failure** in `RuntimeSessionStore` (`retry_pending` / `flush_token_counts` untouched), and **redial on the next call** after any error.
- `ping_interval=None` stays in the `connect(...)` call (D4). No library keepalive.
- Shared `hermes_cli` defaults stay as they are: `private_control_query(..., timeout=5)`, `discover_gateway_endpoint(..., timeout=2.0)`, `query_identify(..., *, timeout)` (required). Only the worker's call sites pass the budget (D3).
- The budget is never duplicated as a literal 120. It is read from `hermes_state.SessionDB._TRANSCRIPT_WRITE_PATIENCE_S` (D1).
- No new retry, no new config key, no new `HERMES_*` env var.
- Tests: no new non-zero `time.sleep` (HX013); wall-clock headroom >= 2 s; reuse the `owner` fixture with its `stall` method in `tests/agent/test_worker_rpc_connection_reuse.py`; no new harness.
- Code commits contain code and tests only. They never contain anything under `docs/superpowers/`, because the [Merge] card cherry-picks them onto `feat/worker-receipt-budget` (D6). Never push. Never touch `~/.hermes/hermes-agent` (the live checkout). Its venv python is only used as `HERMES_PYTHON`.
- One command per Bash call. No `cd`, `&&`, `;`, `$VAR` or `$( )` (cc-run rules).

## Verified facts this plan relies on (checked 2026-11-15 against 1831fe62509d = origin/main 18e659dfc93e + docs)

1. **`_session_ticket` takes no timeout today.** Signature (`hermes_cli/gateway_client.py:284-285`): `_session_ticket(home, endpoint, *, purpose="interactive", scope=None, subject=None, refuse_restart_wait=False) -> str`. It calls `private_control_query(home, request)` twice (`:297` and the restart-wait fallback at `:303`), both with the default `timeout=5` (`:256`).
2. **Other callers and D3 byte-identity:**
   - `private_control_query`: `hermes_cli/webapp_launcher.py:88` (no timeout), `_session_ticket` (`gateway_client.py:297`, `:303`), and tests `tests/gateway/test_control_socket_vectors.py:130` and `tests/gateway/test_control_resolve.py:51`. Its definition and default stay unchanged.
   - `_session_ticket`: `hermes_cli/gateway_runtime_attach.py:126`, `agent/runtime_session_store.py:153`, and tests `tests/gateway/test_multiplex_cold_profile_sidebar_mutation.py:96,104`. These callers pass no `timeout`, so with the new default `timeout=5` they still call `private_control_query(home, request, timeout=5)`, which is the same value as today. The `gateway/control_socket.py:381` `_session_ticket` is the unrelated server-side method.
   - `query_identify` (required keyword `timeout`): `hermes_cli/gateway_runtime.py:235,330`, `gateway_runtime_replace.py:52`, `gateway_runtime_tickets.py:64`, `evals/desktop_gateway_ticket_native/run*.py`, and tests. Only the call at `agent/runtime_session_store.py:150` changes its argument.
   - `discover_gateway_endpoint`: only the worker's own call at `runtime_session_store.py:143` changes its argument. It accepts any finite positive timeout (`gateway_runtime.py:326`).
3. **Reading `_TRANSCRIPT_WRITE_PATIENCE_S` (`hermes_state.py:500`, a `SessionDB` class attribute, 60.0):**
   - There is no import cycle in either direction. Importing `hermes_state` does not load `agent.runtime_session_store`, and `agent.runtime_session_store` / `agent.managed_worker` do not load `hermes_state`.
   - A module-level import is NOT cheap: `hermes_state` costs about 0.70 s cumulative on a cold interpreter (`-X importtime`).
   - Inside the worker child, `run_agent` is already loaded, and the incremental import of `hermes_state` then costs **0.030 s**.
   - **Choice:** a function-level `from hermes_state import SessionDB` inside `worker_receipt_budget()`. This is the smallest change. It is the same late-import pattern as `admission_answer_budget()` (`hermes_cli/kanban_worker_client.py:29-35`), it keeps the source of truth on `SessionDB` (D1, no literal), it is paid once per child on its first RPC, and it does not touch `hermes_state.py`.
   - Moving the constant into a light module was rejected because it would edit the owner's storage facade for no behavioural gain.
4. **Test fixture seams:** `WorkerRPC._connection` imports `_session_ticket`, `query_identify`, `discover_gateway_endpoint` and `websockets.sync.client.connect` inside the function. The `owner` fixture's `monkeypatch.setattr` on those modules is therefore where production reads them. The fixture's fake `ticket(home, endpoint, purpose)` does **not** accept `timeout`, so it must be extended in the same task that starts passing `timeout=` (Task 3).
5. **Baselines:**
   - The test command on the base gives `4 files, 13 tests passed, 0 failed`.
   - `python scripts/check` on the base gives `code health: no measured files changed` / `scripts/check: 19 checks, ok`, which is 0 findings.
   - File sizes are well under the ratchet: `agent/runtime_session_store.py` 497 lines, `hermes_cli/gateway_client.py` 393, `tests/agent/test_worker_rpc_connection_reuse.py` 273.
6. No website/skills/AGENTS.md docs mention the 20 s receipt budget (`grep -rn "worker_receipt_timeout\|receive(20)"` outside `docs/` hits only the code and the test docstring changed below). There are no doc updates beyond D4.

## Test command

Base command plus the two new files (the plan's canonical command; written on one line):

```
env -u HERMES_HOME -u HERMES_PROFILE -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_RUN_ID -u HERMES_KANBAN_WORKSPACE HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_6740e5cd/tmp scripts/run_tests.sh tests/agent/test_worker_rpc_connection_reuse.py tests/agent/test_worker_rpc_proxy.py tests/agent/test_runtime_session_store_proxy.py tests/agent/test_worker_rpc_loop_diagnostic.py tests/agent/test_worker_receipt_budget.py tests/hermes_cli/test_gateway_client_session_ticket_timeout.py
```

Below this is called **TESTCMD**. The runner is file-granular, and `-k <expr>` narrows it.

**Expected final:** `=== Summary: 6 files, 21 tests passed, 0 failed` (13 existing + 8 new: 3 in `test_worker_rpc_connection_reuse.py`, 2 in `test_worker_receipt_budget.py`, 3 in `test_gateway_client_session_ticket_timeout.py`).

## File map

| File | Change |
|---|---|
| `agent/runtime_session_store.py` | Add `WORKER_RECEIPT_MARGIN_S`, `worker_receipt_budget()`. `__call__` uses it for `receive`; `_connection` uses it for discover/identify/ticket/open. Comment D4. |
| `hermes_cli/gateway_client.py` | `_session_ticket(..., timeout: float = 5)` passes `timeout` to both `private_control_query` calls. |
| `tests/agent/test_worker_receipt_budget.py` (new) | D5(b) invariant: the worker never gives up before the owner. |
| `tests/hermes_cli/test_gateway_client_session_ticket_timeout.py` (new) | `_session_ticket` timeout pass-through; other callers keep 5 s. |
| `tests/agent/test_worker_rpc_connection_reuse.py` | Fixture records timeouts. D5(a) two stall tests. D5(c) connect-path test. D4 docstring. |

---

### Task 1: `worker_receipt_budget()` governs the receipt wait (D1, D2, D4, D5a, D5b)

**Files:**
- Modify: `agent/runtime_session_store.py` (after `class WorkerPersistenceError` at `:46-47`; `:156-157` comment; `:197` `receive(20)`)
- Create: `tests/agent/test_worker_receipt_budget.py`
- Modify: `tests/agent/test_worker_rpc_connection_reuse.py` (docstring `:222-224`; append two tests after `test_owner_stall_shorter_than_the_receipt_budget_does_not_fail_on_a_ping_deadline`)

**Interfaces:**
- Produces: `agent.runtime_session_store.WORKER_RECEIPT_MARGIN_S: float = 60.0`; `agent.runtime_session_store.worker_receipt_budget() -> float`. Callers inside the module look it up as a module global at call time, so tests can `monkeypatch.setattr(runtime_session_store, 'worker_receipt_budget', ...)`.

- [ ] **Step 1: Write the invariant tests (new file)** `tests/agent/test_worker_receipt_budget.py`:

```python
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
```

- [ ] **Step 2: Write the stall tests.** In `tests/agent/test_worker_rpc_connection_reuse.py`, append directly after `test_owner_stall_shorter_than_the_receipt_budget_does_not_fail_on_a_ping_deadline` (it ends with `assert owner.connections == 1`):

```python
def test_an_owner_stall_inside_the_receipt_budget_returns_the_receipt(owner, tmp_path, monkeypatch):
    """Scaled down: a 3 s budget stands for the real one (the owner's write patience plus the margin),
    a 1 s stall for an owner that answers late but inside it. Same connection, nothing resent."""
    from agent import runtime_session_store
    from agent.runtime_session_store import WorkerRPC
    monkeypatch.setattr(runtime_session_store, 'worker_receipt_budget', lambda: 3.0)
    rpc = WorkerRPC(tmp_path)
    assert rpc('worker.persist')['n'] == 1
    started = time.monotonic()
    assert rpc('stall', seconds=1.0) == {'n': 2, 'seconds': 1.0}
    assert time.monotonic() - started >= 0.9
    assert owner.requests == ['worker.persist', 'stall']
    assert owner.connections == 1


def test_an_owner_stall_past_the_receipt_budget_fails_once_and_is_not_resent(owner, tmp_path, monkeypatch):
    """The budget, not a literal, ends the wait: a 3 s budget gives up on a 6 s stall after ~3 s with
    ``worker_receipt_timeout``, drops the link and never resends (the outcome is unknown)."""
    from agent import runtime_session_store
    from agent.runtime_session_store import WorkerRPC
    monkeypatch.setattr(runtime_session_store, 'worker_receipt_budget', lambda: 3.0)
    rpc = WorkerRPC(tmp_path)
    assert rpc('worker.persist')['n'] == 1
    started = time.monotonic()
    with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
        rpc('stall', seconds=6.0)
    assert 2.9 <= time.monotonic() - started < 5.5
    assert owner.requests == ['worker.persist', 'stall']
    assert rpc.link is None
```

(Spec D5(a) suggests 1.5 s / 1.0 s / 2.0 s. The plan uses 3.0 s / 1.0 s / 6.0 s because `tests/AGENTS.md` requires >= 2 s wall-clock headroom on a loaded runner. The test stays the same in substance.)

- [ ] **Step 3: Update the D4 docstring.** In the same file, replace the docstring of `test_owner_stall_shorter_than_the_receipt_budget_does_not_fail_on_a_ping_deadline`:

```python
    """The receipt budget (``worker_receipt_budget()``: the owner's write patience plus
    ``WORKER_RECEIPT_MARGIN_S``) is the only per-call deadline: a library keepalive whose pong is held
    by the same stall must not close the connection first. The library defaults (20 s / 20 s) are
    scaled down so an inherited keepalive would fire inside the stall."""
```

- [ ] **Step 4: Run, expect RED.** Run TESTCMD without its last path (`tests/hermes_cli/test_gateway_client_session_ticket_timeout.py` does not exist until Task 2).
  Expected: 4 failed:
  - `test_worker_receipt_budget.py::test_the_worker_never_gives_up_before_the_owner` and `::test_the_budget_follows_the_owners_write_patience` fail with `ImportError: cannot import name 'WORKER_RECEIPT_MARGIN_S'`.
  - `test_worker_rpc_connection_reuse.py::test_an_owner_stall_inside_the_receipt_budget_returns_the_receipt` and `::test_an_owner_stall_past_the_receipt_budget_fails_once_and_is_not_resent` fail with `AttributeError: ... has no attribute 'worker_receipt_budget'`.

- [ ] **Step 5: Implement.** In `agent/runtime_session_store.py`, insert after `class WorkerPersistenceError(RuntimeError):\n    pass` (two blank lines before and after):

```python
# Owner-loop stalls under host load come on top of the owner's state.db write patience.
WORKER_RECEIPT_MARGIN_S = 60.0


def worker_receipt_budget():
    """Seconds a worker waits on each owner RPC step (connect and receipt): the longest the owner
    may hold the write on its state.db lock (``apply_worker_receipt`` runs under
    ``SessionDB._TRANSCRIPT_WRITE_PATIENCE_S``) plus ``WORKER_RECEIPT_MARGIN_S``, so the worker never
    gives up on a write the owner still commits. Mirrors ``admission_answer_budget``."""
    # Late: no import cycle either way; ~30 ms in a child that already loaded run_agent, ~0.7 s cold.
    from hermes_state import SessionDB
    return SessionDB._TRANSCRIPT_WRITE_PATIENCE_S + WORKER_RECEIPT_MARGIN_S
```

  In `WorkerRPC.__call__`, replace the line

```python
                response = link.receive(20)
```

  with

```python
                response = link.receive(worker_receipt_budget())
```

  In `WorkerRPC._connection`, replace the two comment lines above `self.link = _OwnerLink(connect(...` (D4):

```python
        # No library keepalive: its pong deadline would run independently of (and could fail) a call
        # inside worker_receipt_budget(); that budget and the observed close are the liveness checks.
```

  Leave `ping_interval=None` and everything else in `__call__` byte-identical.

- [ ] **Step 6: Run, expect GREEN for this task.** Run TESTCMD without its last path.
  Expected: `5 files, 17 tests passed, 0 failed` (13 + 2 + 2).

- [ ] **Step 7: Commit** (code and tests only):

```
git add agent/runtime_session_store.py tests/agent/test_worker_receipt_budget.py tests/agent/test_worker_rpc_connection_reuse.py
```

```
git commit -m "fix(worker): the receipt wait covers the owner's own write patience" -m "WorkerRPC waited a fixed 20 s for a worker.persist receipt while the owner may hold the write 60 s on its state.db lock plus loop stalls; under host load managed workers died with worker_receipt_timeout on writes the owner then committed. worker_receipt_budget() = SessionDB._TRANSCRIPT_WRITE_PATIENCE_S + WORKER_RECEIPT_MARGIN_S (120 s)." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `_session_ticket` accepts a timeout and passes it through (D3, gateway side)

**Files:**
- Modify: `hermes_cli/gateway_client.py:284-305`
- Create: `tests/hermes_cli/test_gateway_client_session_ticket_timeout.py`

**Interfaces:**
- Produces: `hermes_cli.gateway_client._session_ticket(home: Path, endpoint, *, purpose="interactive", scope=None, subject=None, refuse_restart_wait=False, timeout: float = 5) -> str`. Both `private_control_query` calls receive `timeout=timeout`.

- [ ] **Step 1: Write the tests** `tests/hermes_cli/test_gateway_client_session_ticket_timeout.py`:

```python
"""``_session_ticket`` hands a caller's timeout to the control query; every other caller keeps 5 s.

A managed worker mints its adoption ticket under its receipt budget
(``agent.runtime_session_store.worker_receipt_budget``), not the shared 5 s default.
"""
import json
from types import SimpleNamespace

from hermes_cli import gateway_client

_ENDPOINT = SimpleNamespace(profile_id='profile', instance_id='instance', control_home=None)
_GRANT = json.dumps({'ok': True, 'protocol': 1, 'id': 1, 'result': {
    'profile_id': 'profile', 'instance_id': 'instance', 'runtime_protocol': 1,
    'ticket': 'ticket-1'}}).encode() + b'\n'


def _control_socket(monkeypatch, answers):
    """Stands in for the owner's control socket; records the timeout of every query."""
    seen = []

    def query(home, request, timeout=5):
        seen.append(timeout)
        return answers.pop(0)
    monkeypatch.setattr(gateway_client, 'private_control_query', query)
    return seen


def test_other_callers_keep_the_five_second_default(monkeypatch, tmp_path):
    seen = _control_socket(monkeypatch, [_GRANT])
    assert gateway_client._session_ticket(tmp_path, _ENDPOINT, purpose='native-http') == 'ticket-1'
    assert seen == [5]


def test_an_explicit_timeout_reaches_the_control_query(monkeypatch, tmp_path):
    seen = _control_socket(monkeypatch, [_GRANT])
    assert gateway_client._session_ticket(tmp_path, _ENDPOINT, purpose='worker-adoption',
                                          timeout=120.0) == 'ticket-1'
    assert seen == [120.0]


def test_the_restart_wait_fallback_query_keeps_the_timeout(monkeypatch, tmp_path):
    refused = json.dumps({'ok': False, 'error': gateway_client._UNKNOWN_TICKET_PARAMS}).encode() + b'\n'
    seen = _control_socket(monkeypatch, [refused, _GRANT])
    assert gateway_client._session_ticket(tmp_path, _ENDPOINT, purpose='worker-adoption',
                                          refuse_restart_wait=True, timeout=120.0) == 'ticket-1'
    assert seen == [120.0, 120.0]
```

- [ ] **Step 2: Run, expect RED.** Run TESTCMD restricted to the single path `tests/hermes_cli/test_gateway_client_session_ticket_timeout.py`.
  Expected: `test_an_explicit_timeout_reaches_the_control_query` and `test_the_restart_wait_fallback_query_keeps_the_timeout` FAIL with `TypeError: _session_ticket() got an unexpected keyword argument 'timeout'`. `test_other_callers_keep_the_five_second_default` passes, because it guards today's behaviour.

- [ ] **Step 3: Implement.** In `hermes_cli/gateway_client.py`, change the signature:

```python
def _session_ticket(home: Path, endpoint, *, purpose="interactive", scope=None, subject=None,
                    refuse_restart_wait=False, timeout: float = 5) -> str:
```

  Replace `answer = private_control_query(home, request)` with

```python
    answer = private_control_query(home, request, timeout=timeout)
```

  and the fallback

```python
        answer = private_control_query(home, json.dumps(
            {"protocol": 1, "id": 1, "verb": "session-ticket", "params": params}).encode() + b"\n")
```

  with

```python
        answer = private_control_query(home, json.dumps(
            {"protocol": 1, "id": 1, "verb": "session-ticket", "params": params}).encode() + b"\n",
            timeout=timeout)
```

  Do not change `private_control_query`'s signature or default.

- [ ] **Step 4: Run, expect GREEN.** Run TESTCMD.
  Expected: `6 files, 20 tests passed, 0 failed` (17 + 3).

- [ ] **Step 5: Commit** (code and tests only):

```
git add hermes_cli/gateway_client.py tests/hermes_cli/test_gateway_client_session_ticket_timeout.py
```

```
git commit -m "fix(gateway-client): _session_ticket takes an explicit control-query timeout" -m "Keyword-only, default 5 (today's private_control_query default), so every existing caller is unchanged; the managed worker passes its receipt budget." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The connect path runs under the same budget (D3 worker side, D5c)

**Files:**
- Modify: `agent/runtime_session_store.py` (`WorkerRPC._connection`, `:143-159` on the base)
- Modify: `tests/agent/test_worker_rpc_connection_reuse.py` (`owner` fixture `:59-60`, `:98-110`; append one test)

**Interfaces:**
- Consumes: `worker_receipt_budget()` (Task 1); `_session_ticket(..., timeout=...)` (Task 2).
- Produces: the `owner` fixture state gains `timeouts: dict`, with keys `'discover'`, `'identify'`, `'ticket'` filled by the fakes, and `'open'` filled by a test's own `connect` wrapper.

- [ ] **Step 1: Extend the fixture.** In `tests/agent/test_worker_rpc_connection_reuse.py`, change the `state = SimpleNamespace(...)` line so that it ends with `live=None, timeouts={})`:

```python
    state = SimpleNamespace(connections=0, tickets=0, requests=[], drop=None, closed=threading.Event(),
                            pongs=[], flooded=threading.Event(), relay=None, live=None, timeouts={})
```

  Replace the fake `ticket` and the three `monkeypatch.setattr` lines for discovery/identify/ticket. That is, everything from `    def ticket(home, endpoint, purpose):` down to `        monkeypatch.setattr(gateway_client, '_session_ticket', ticket)`, keeping the `with serve(...)` / `threading.Thread(...)` / `state.relay = ...` / `endpoint = ...` lines in place. The result is:

```python
    def ticket(home, endpoint, purpose, timeout=None):  # None: the caller passed no timeout
        assert purpose == 'worker-adoption'
        state.tickets += 1
        state.timeouts['ticket'] = timeout
        return f'ticket-{state.tickets}'

    def identify(home, timeout):
        state.timeouts['identify'] = timeout
        return {'pid': -1}

    with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        state.relay = _Relay(server.socket.getsockname()[1])
        endpoint = SimpleNamespace(api_origin=f'http://127.0.0.1:{state.relay.port}', control_home=None)

        def discover(home, timeout):
            state.timeouts['discover'] = timeout
            return SimpleNamespace(state='ready', endpoint=endpoint)
        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover)
        monkeypatch.setattr(discovery, 'query_identify', identify)
        monkeypatch.setattr(gateway_client, '_session_ticket', ticket)
```

  (The `try: yield state / finally: ...` block after it stays unchanged.)

- [ ] **Step 2: Write the connect-path test.** Append at the end of the file:

```python
def test_the_connect_path_waits_the_receipt_budget_on_every_step(owner, tmp_path, monkeypatch):
    """Discovery, identify, the adoption ticket and the WebSocket open each run under the receipt
    budget: an owner whose loop stalls at turn start is as slow to admit the link as to answer on it."""
    from websockets.sync import client
    from agent.runtime_session_store import WorkerRPC, worker_receipt_budget
    real = client.connect

    def connect(*args, **kwargs):
        owner.timeouts['open'] = kwargs.get('open_timeout')
        return real(*args, **kwargs)
    monkeypatch.setattr(client, 'connect', connect)
    assert WorkerRPC(tmp_path)('worker.persist')['n'] == 1
    assert owner.timeouts == dict.fromkeys(('discover', 'identify', 'ticket', 'open'), worker_receipt_budget())
```

- [ ] **Step 3: Run, expect RED.** Run TESTCMD.
  Expected: exactly 1 failed, `test_worker_rpc_connection_reuse.py::test_the_connect_path_waits_the_receipt_budget_on_every_step`. The assertion diff shows `'discover': 5, 'identify': 5, 'ticket': None, 'open': 5` against `120.0` each. The other 20 pass, which shows that the fixture change is benign.

- [ ] **Step 4: Implement.** In `WorkerRPC._connection`, read the budget once after the imports and pass it to all four steps. The body from `discovery = ...` to the `connect(...)` call becomes:

```python
        budget = worker_receipt_budget()
        discovery = discover_gateway_endpoint(self.home, timeout=budget)
        if discovery.state == 'ready' and discovery.endpoint is not None:
            self.endpoint = discovery.endpoint
        elif discovery.state != 'draining' or self.endpoint is None:
            raise WorkerPersistenceError('owner_unavailable')
        from hermes_cli.gateway_runtime import control_home_for
        endpoint = self.endpoint
        descriptor = query_identify(control_home_for(self.home, endpoint), timeout=budget)
        if descriptor.get('pid') == os.getpid():
            raise WorkerPersistenceError('synchronous_self_rpc')
        ticket = _session_ticket(self.home, endpoint, purpose='worker-adoption', timeout=budget)
        url, protocols = gateway_ws_target(endpoint, ticket)
        # The owner is loopback, like connect_gateway's peer: an inherited HTTPS_PROXY must not carry it.
        # No library keepalive: its pong deadline would run independently of (and could fail) a call
        # inside worker_receipt_budget(); that budget and the observed close are the liveness checks.
        self.link = _OwnerLink(connect(url, subprotocols=protocols, open_timeout=budget, close_timeout=1,
                                       max_size=8 * 1024 * 1024, proxy=None, ping_interval=None))
        return self.link
```

  (`close_timeout=1`, `max_size`, `proxy=None` and `ping_interval=None` stay unchanged. A refused or missing socket still fails at once, so only a silent owner uses the full budget. In the worst case, a fully silent owner that answers none of the four steps is detected after 4 × the budget. D3 accepts that.)

- [ ] **Step 5: Run, expect GREEN.** Run TESTCMD.
  Expected: `=== Summary: 6 files, 21 tests passed, 0 failed`.

- [ ] **Step 6: Commit** (code and tests only):

```
git add agent/runtime_session_store.py tests/agent/test_worker_rpc_connection_reuse.py
```

```
git commit -m "fix(worker): the owner connect path waits the receipt budget too" -m "Discovery, identify, the adoption ticket and the WebSocket open each used 5 s; 2 of the 28 crashes on 2026-11-15 were TimeoutError there at turn start under the same owner-loop stalls." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: RED proof on the unmodified base and the mutants M1-M3 (D5)

This task changes no tracked file and has no commit. Paste the outputs into the card handoff. All work happens in DETACHED scratch worktrees under `/home/birk/scratch/t_6740e5cd/`. Never switch the card worktree.

**RED proof: the new tests on origin/main 18e659dfc93e**

- [ ] **Step 1:** `git worktree add --detach /home/birk/scratch/t_6740e5cd/red 18e659dfc93e`
- [ ] **Step 2: Copy the three test files from the card worktree.** Run one `cp` per call, from `tests/agent/test_worker_receipt_budget.py`, `tests/agent/test_worker_rpc_connection_reuse.py` and `tests/hermes_cli/test_gateway_client_session_ticket_timeout.py` to the same relative path under `/home/birk/scratch/t_6740e5cd/red/`.
- [ ] **Step 3: Run** the TESTCMD with `scripts/run_tests.sh` replaced by `/home/birk/scratch/t_6740e5cd/red/scripts/run_tests.sh`, and with `HERMES_TEST_FILE_RETRIES=0` added to the `env` assignments. The runner `cd`s to its own repo root, so the relative test paths resolve in the scratch tree.
  Expected: 14 passed, **7 failed**. The failing ids are exactly:
  - `tests/agent/test_worker_rpc_connection_reuse.py::test_an_owner_stall_inside_the_receipt_budget_returns_the_receipt` (AttributeError: no `worker_receipt_budget`)
  - `tests/agent/test_worker_rpc_connection_reuse.py::test_an_owner_stall_past_the_receipt_budget_fails_once_and_is_not_resent` (AttributeError)
  - `tests/agent/test_worker_rpc_connection_reuse.py::test_the_connect_path_waits_the_receipt_budget_on_every_step` (ImportError)
  - `tests/agent/test_worker_receipt_budget.py::test_the_worker_never_gives_up_before_the_owner` (ImportError)
  - `tests/agent/test_worker_receipt_budget.py::test_the_budget_follows_the_owners_write_patience` (ImportError)
  - `tests/hermes_cli/test_gateway_client_session_ticket_timeout.py::test_an_explicit_timeout_reaches_the_control_query` (TypeError: unexpected keyword `timeout`)
  - `tests/hermes_cli/test_gateway_client_session_ticket_timeout.py::test_the_restart_wait_fallback_query_keeps_the_timeout` (TypeError)

  The 9 pre-existing tests in `test_worker_rpc_connection_reuse.py` pass with the modified fixture, and so does `test_other_callers_keep_the_five_second_default`.
- [ ] **Step 4:** `git worktree remove --force /home/birk/scratch/t_6740e5cd/red`

**Mutants: each one on a fresh detached copy of the fixed tree**

- [ ] **Step 5:** `git rev-parse HEAD` in the card worktree. Call the printed SHA `FIX`.
- [ ] **Step 6: M1, `receive(20)` restored.**
  1. `git worktree add --detach /home/birk/scratch/t_6740e5cd/m1 FIX`
  2. Edit `/home/birk/scratch/t_6740e5cd/m1/agent/runtime_session_store.py`: `link.receive(worker_receipt_budget())` → `link.receive(20)`.
  3. Run the scratch-tree TESTCMD restricted to `tests/agent/test_worker_rpc_connection_reuse.py -k past_the_receipt_budget`.

  Expected: **FAIL** of `test_an_owner_stall_past_the_receipt_budget_fails_once_and_is_not_resent` with `Failed: DID NOT RAISE <class 'TimeoutError'>`. The reply arrives after the 6 s stall, inside the literal 20 s.
- [ ] **Step 7: M2, margin set to 0.**
  1. `git worktree add --detach /home/birk/scratch/t_6740e5cd/m2 FIX`
  2. Edit `WORKER_RECEIPT_MARGIN_S = 60.0` → `WORKER_RECEIPT_MARGIN_S = 0.0`.
  3. Run the scratch-tree TESTCMD restricted to `tests/agent/test_worker_receipt_budget.py`.

  Expected: **FAIL** of `test_the_worker_never_gives_up_before_the_owner` with `assert 60.0 >= (60.0 + 1)`.
- [ ] **Step 8: M3a, the worker's ticket query keeps its default.**
  1. `git worktree add --detach /home/birk/scratch/t_6740e5cd/m3a FIX`
  2. Edit `_session_ticket(self.home, endpoint, purpose='worker-adoption', timeout=budget)` → `_session_ticket(self.home, endpoint, purpose='worker-adoption')`.
  3. Run the scratch-tree TESTCMD restricted to `tests/agent/test_worker_rpc_connection_reuse.py -k connect_path`.

  Expected: **FAIL** of `test_the_connect_path_waits_the_receipt_budget_on_every_step` with `'ticket': None` vs `120.0`.
- [ ] **Step 9: M3b, `_session_ticket` takes the timeout but does not pass it on** (the query keeps its default 5 s).
  1. `git worktree add --detach /home/birk/scratch/t_6740e5cd/m3b FIX`
  2. Edit `/home/birk/scratch/t_6740e5cd/m3b/hermes_cli/gateway_client.py`: remove `, timeout=timeout` from the first `private_control_query(...)` call and `,\n            timeout=timeout` from the fallback call.
  3. Run the scratch-tree TESTCMD restricted to `tests/hermes_cli/test_gateway_client_session_ticket_timeout.py`.

  Expected: **FAIL** of `test_an_explicit_timeout_reaches_the_control_query` (`[5] == [120.0]`) and of `test_the_restart_wait_fallback_query_keeps_the_timeout` (`[5, 5] == [120.0, 120.0]`).
- [ ] **Step 10: Remove the mutant worktrees**, one call each: `git worktree remove --force /home/birk/scratch/t_6740e5cd/m1` (then `m2`, `m3a`, `m3b`). Then run `git worktree prune`.

If any mutant stays green, stop. The tests do not pin the decision, so fix the test before you go on, and do not weaken the mutant.

---

### Task 5: Final verification

- [ ] **Step 1:** Run TESTCMD. Expected: `=== Summary: 6 files, 21 tests passed, 0 failed`.
- [ ] **Step 2:** `python scripts/check` (timeout 900000 ms). Base record: `code health: no measured files changed` / `scripts/check: 19 checks, ok` (0 findings). Required now: `scripts/check: 19 checks, ok` with the health section showing no new finding for `agent/runtime_session_store.py`, `hermes_cli/gateway_client.py` or the test files. Any finding is a blocker. Fix the code. Do not waive the finding.
- [ ] **Step 3:** `git diff 18e659dfc93e --stat -- . ":(exclude)docs"` lists exactly the 5 files in the file map.
- [ ] **Step 4:** `git log --stat 18e659dfc93e..HEAD` shows that none of the three code commits touches `docs/superpowers/`. The plan and spec commits are separate.
- [ ] **Step 5: Hand-off note for the [Merge] card (D6).**
  - It creates `feat/worker-receipt-budget` from origin/main and cherry-picks the three code commits.
  - It pushes WITHOUT `docs/superpowers/` and adds the feat to `~/.hermes/hermes-agent-features.txt`.
  - The live effect needs `hermes-up` plus a gateway restart, which is Birk's step.
  - If `worker_receipt_timeout` recurs after this, the stall exceeded 120 s. That is the signal for a loop-stall investigation card (spec §6), not for a larger margin.

---

## Spec coverage

| Decision | Task |
|---|---|
| D1 constant + function, read from `SessionDB` | Task 1 Step 5; Verified fact 3 |
| D2 `receive(worker_receipt_budget())`, semantics unchanged | Task 1 Step 5; Global Constraints |
| D3 discover/identify/ticket/open under the budget; `_session_ticket` keyword `timeout`, default 5; shared defaults untouched | Task 2, Task 3; Verified facts 1-2 |
| D4 `ping_interval=None` stays; comment and docstring name the constant | Task 1 Steps 3, 5; Task 3 Step 4 |
| D5(a) stall inside/past budget, owner fixture `stall` | Task 1 Step 2 |
| D5(b) budget == patience + margin, >= patience + 1 | Task 1 Step 1 |
| D5(c) connect-path spies | Task 3 Steps 1-2 |
| D5 RED on base, M1-M3 | Task 4 |
| D6 delivery, no push, no docs in code commits | Global Constraints; Task 5 Steps 4-5 |
