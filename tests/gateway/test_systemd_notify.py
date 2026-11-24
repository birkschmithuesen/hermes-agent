"""Tests for the optional systemd event-loop watchdog protocol."""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

pytestmark = pytest.mark.platforms("linux")


# abstract (NUL-prefixed) AF_UNIX names are a Linux kernel feature
def test_notify_supports_systemd_abstract_socket(monkeypatch):
    name = "\0hermes-test-notify"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(name)
    receiver.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", "@hermes-test-notify")

    try:
        from gateway.systemd_notify import notify

        assert notify("WATCHDOG=1") is True
        assert receiver.recv(4096) == b"WATCHDOG=1"
    finally:
        receiver.close()


def test_notify_uses_nonblocking_datagram_send(monkeypatch):
    calls: list[object] = []

    class _Sender:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def setblocking(self, value):
            calls.append(("setblocking", value))

        def connect(self, address):
            calls.append(("connect", address))

        def send(self, payload):
            calls.append(("send", payload))

    import gateway.systemd_notify as notify_mod

    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.setattr(notify_mod.socket, "socket", lambda *_args: _Sender())

    assert notify_mod.notify("READY=1") is True
    assert calls[0] == ("setblocking", False)


def test_notify_refuses_when_watchdog_pid_is_a_different_process(monkeypatch):
    """systemd exports WATCHDOG_PID as the process it expects to feed the watchdog
    (sd_watchdog_enabled(3)); a process whose own pid disagrees must never send
    READY=1/WATCHDOG=1 on its behalf — that is exactly the NotifyAccess=main
    rejection ("Got notification message from PID X, but reception only
    permitted for main PID") a stray or forked sender would trigger."""
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    name = "\0hermes-test-notify-wrong-pid"
    receiver.bind(name)
    receiver.settimeout(0.2)
    monkeypatch.setenv("NOTIFY_SOCKET", "@hermes-test-notify-wrong-pid")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))

    try:
        from gateway.systemd_notify import notify

        assert notify("WATCHDOG=1") is False
        with pytest.raises(socket.timeout):
            receiver.recv(4096)
    finally:
        receiver.close()


def test_notify_sends_when_watchdog_pid_matches_this_process(monkeypatch):
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    name = "\0hermes-test-notify-own-pid"
    receiver.bind(name)
    receiver.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", "@hermes-test-notify-own-pid")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))

    try:
        from gateway.systemd_notify import notify

        assert notify("WATCHDOG=1") is True
        assert receiver.recv(4096) == b"WATCHDOG=1"
    finally:
        receiver.close()


@pytest.mark.asyncio
async def test_watchdog_sends_ready_heartbeat_and_stopping(monkeypatch):
    calls: list[str] = []
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-notify")
    monkeypatch.setenv("WATCHDOG_USEC", "20000")

    import gateway.systemd_notify as notify_mod

    monkeypatch.setattr(
        notify_mod, "notify", lambda message: calls.append(message) or True
    )
    watchdog = notify_mod.SystemdWatchdog(lag_tolerance_seconds=1.0)

    assert watchdog.start() is True
    assert watchdog.ready("Gateway running") is True
    await asyncio.sleep(0.04)
    await watchdog.stop()

    assert any(message.startswith("READY=1") for message in calls)
    assert "WATCHDOG=1" in calls
    assert calls[-1] == "STOPPING=1"
    assert watchdog.unhealthy is False


