"""pm downloader routes pinned (full-sha256) sources through the local
download broker when security.download_broker is configured.

Covers: broker success writes a verified file; broker 422 raises and leaves
no file; broker unreachable falls back to the direct source; feature off
never contacts the broker.
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hermes_cli.config import get_config_path
from pm.downloader import Download, DownloadError, Source

from tests.pm._range_server import RangeHandler as _Handler, url as _url
from tests.pm._range_server import dl_server as dl_server  # noqa: F401


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _set_broker_config(broker_url: str) -> None:
    get_config_path().write_text(
        f"security:\n  download_broker: {broker_url!r}\n", encoding="utf-8")


class _BrokerHandler(BaseHTTPRequestHandler):
    """A tiny fake of hermes-dlbroker's POST /download contract."""

    payload: bytes = b""
    mode: str = "ok"  # ok | refuse_422 | refuse_403 | upstream_502

    def log_message(self, *args):  # noqa: A002 - silence request logging
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        assert body.get("url")
        if self.mode == "refuse_422":
            self.send_error(422, "checksum mismatch")
            return
        if self.mode == "refuse_403":
            self.send_error(403, "refused by policy")
            return
        if self.mode == "upstream_502":
            self.send_error(502, "upstream failure")
            return
        payload = self.payload
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Sha256", _sha(payload))
        self.send_header("X-Verified", "true")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


@pytest.fixture
def broker_server():
    _BrokerHandler.payload = b""
    _BrokerHandler.mode = "ok"
    server = HTTPServer(("127.0.0.1", 0), _BrokerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _broker_url(server) -> str:
    return f"http://127.0.0.1:{server.server_port}"


def test_broker_success_writes_verified_file(tmp_path, broker_server):
    payload = b"pinned package bytes" * 50
    _BrokerHandler.payload = payload
    _set_broker_config(_broker_url(broker_server))
    dest = tmp_path / "pkg.tgz"
    # Direct source deliberately unreachable (closed loopback port) to prove
    # the broker path, not a direct fallback, served these bytes.
    source = Source("https://example.invalid/pkg.tgz", dest, _sha(payload))
    ticks = []
    result = Download([source], partials_dir=tmp_path / "partials").run(
        progress=lambda done, total, ranges: ticks.append((done, total)))
    assert result == [dest]
    assert dest.read_bytes() == payload
    assert ticks, "progress must still tick when the broker path is used"
    assert ticks[-1][0] == len(payload)


def test_broker_422_is_a_clear_error_and_leaves_no_file(tmp_path, broker_server):
    _BrokerHandler.mode = "refuse_422"
    _set_broker_config(_broker_url(broker_server))
    dest = tmp_path / "pkg.tgz"
    source = Source("https://example.invalid/pkg.tgz", dest, "a" * 64)
    with pytest.raises(DownloadError, match="broker"):
        Download([source], partials_dir=tmp_path / "partials").run()
    assert not dest.exists()


def test_broker_403_is_a_clear_error_and_leaves_no_file(tmp_path, broker_server):
    _BrokerHandler.mode = "refuse_403"
    _set_broker_config(_broker_url(broker_server))
    dest = tmp_path / "pkg.tgz"
    source = Source("https://example.invalid/pkg.tgz", dest, "a" * 64)
    with pytest.raises(DownloadError, match="broker"):
        Download([source], partials_dir=tmp_path / "partials").run()
    assert not dest.exists()


def test_broker_down_falls_back_to_direct_source(tmp_path, dl_server, broker_server):
    payload = b"served directly instead"
    _Handler.payloads = {"/pkg.tgz": payload}
    # Broker refuses with a 502 (upstream failure) -- unreachable/failed,
    # not a policy refusal -- so the direct source chain must still run.
    _BrokerHandler.mode = "upstream_502"
    _set_broker_config(_broker_url(broker_server))
    dest = tmp_path / "pkg.tgz"
    source = Source(_url(dl_server, "/pkg.tgz"), dest, _sha(payload))
    result = Download([source], partials_dir=tmp_path / "partials").run()
    assert result == [dest]
    assert dest.read_bytes() == payload


def test_broker_unreachable_connection_refused_falls_back_to_direct(tmp_path, dl_server):
    payload = b"direct bytes, broker never came up"
    _Handler.payloads = {"/pkg.tgz": payload}
    # Nothing listens on this port: broker is "down" in the connection-refused sense.
    _set_broker_config("http://127.0.0.1:1")
    dest = tmp_path / "pkg.tgz"
    source = Source(_url(dl_server, "/pkg.tgz"), dest, _sha(payload))
    result = Download([source], partials_dir=tmp_path / "partials").run()
    assert result == [dest]
    assert dest.read_bytes() == payload


def test_feature_off_never_contacts_broker(tmp_path, dl_server, broker_server, monkeypatch):
    payload = b"direct bytes only, broker disabled"
    _Handler.payloads = {"/pkg.tgz": payload}
    contacted = []
    original = _BrokerHandler.do_POST

    def spy(self):
        contacted.append(self.path)
        original(self)

    monkeypatch.setattr(_BrokerHandler, "do_POST", spy)
    # No security.download_broker key at all -> default "" -> feature off.
    get_config_path().write_text("{}\n", encoding="utf-8")
    dest = tmp_path / "pkg.tgz"
    source = Source(_url(dl_server, "/pkg.tgz"), dest, _sha(payload))
    result = Download([source], partials_dir=tmp_path / "partials").run()
    assert result == [dest]
    assert dest.read_bytes() == payload
    assert contacted == []


def test_broker_not_used_without_a_full_pinned_sha256(tmp_path, dl_server, broker_server, monkeypatch):
    """Unpinned (model-catalog) sources never route through the broker --
    the broker's own contract requires a sha256 to pre-verify against."""
    payload = b"unpinned catalog bytes"
    _Handler.payloads = {"/model.bin": payload}
    contacted = []
    original = _BrokerHandler.do_POST

    def spy(self):
        contacted.append(self.path)
        original(self)

    monkeypatch.setattr(_BrokerHandler, "do_POST", spy)
    _set_broker_config(_broker_url(broker_server))
    dest = tmp_path / "model.bin"
    source = Source(_url(dl_server, "/model.bin"), dest)  # sha256="" by default
    result = Download([source], partials_dir=tmp_path / "partials").run()
    assert result == [dest]
    assert dest.read_bytes() == payload
    assert contacted == []
