#!/usr/bin/env python3
"""Expiry monitor for the LIVE delivered TLS certificate.

Purpose (card t_b8b9a6a7, acceptance point 5): the ACME path for
office.artesmobiles.art is broken (port 80 closed from outside). The current
certificate expires on 2026-11-16; snap.certbot.renew.timer will start
failing twice a day around 2026-10-17 -- silently, because nobody reads its
output. This watchdog makes that failure visible BEFORE the service goes down.

LLM-free (`no_agent=True`): pure stdlib, the script IS the job.
Delivery semantics this relies on:
  empty output    -> silent (normal case, almost always)
  text            -> delivered verbatim
  exit != 0       -> error alarm (a broken watchdog must not stay silent)

🔴 This measures the LIVE delivered chain at the port, NOT the file under
/etc/letsencrypt/live/. A renewed certificate that the deploy hook failed to
bring into the container would look fresh on disk while still expiring at the
port -- exactly the failure mode that matters here. (The deploy hook had
demonstrably never run, see skill tls-certificate-troubleshooting.)
"""
from __future__ import annotations

import datetime as dt
import os
import re
import socket
import ssl
import subprocess
import sys
from pathlib import Path

# --- Configuration -------------------------------------------------------------

HOST = os.environ.get("CERT_WATCH_HOST", "office.artesmobiles.art")
PORT = int(os.environ.get("CERT_WATCH_PORT", "443"))

# Threshold per acceptance point 5: report below 21 days.
# Deliberately > certbot's own renew_before_expiry = 30 days MINUS a safety
# margin: certbot starts trying to renew on day 30. If this watchdog only
# reports at 21, ~9 days of renewal attempts have already silently failed
# -- that is exactly the signal we want to hear, and three weeks are still
# left to act.
WARN_DAYS = int(os.environ.get("CERT_WATCH_WARN_DAYS", "21"))

# Cooldown: not the same warning every day. But NO permanent silencing
# -- the situation can get worse (skill llm-free-watchdog-jobs: the abort
# criterion belongs at the output, not the reporting). If the situation
# becomes more urgent (next escalation step reached), it reports again
# immediately.
COOLDOWN_H = float(os.environ.get("CERT_WATCH_COOLDOWN_H", "24"))

# Escalation steps in days of remaining lifetime. If the remaining lifetime
# drops below a new step, the cooldown is ignored and it reports immediately.
ESCALATION_STEPS = (21, 14, 7, 3, 1, 0)

CONNECT_TIMEOUT = float(os.environ.get("CERT_WATCH_TIMEOUT", "15"))

VAR = Path(os.environ.get(
    "CERT_WATCH_VAR",
    Path.home() / ".hermes" / "profiles" / "birk" / "var" / "cert-expiry-watch",
))
STATE = VAR / "last_alert.txt"

# --- Measurement -----------------------------------------------------------


class ProbeError(RuntimeError):
    """The measurement itself failed -- this is NEVER 'all good'."""


def fetch_live_notafter(host: str, port: int, timeout: float) -> dt.datetime:
    """Fetches notAfter from the leaf certificate delivered at the port.

    Important: a normal ssl handshake with verification only returns the
    parsed certificate on success. But we want to be able to read an
    expiry date even when verification fails PRECISELY because of
    expiry -- otherwise the watchdog stays silent exactly in the real
    emergency. So the DER is fetched unverified and parsed separately.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        raise ProbeError(f"connection to {host}:{port} failed: {exc}") from exc

    if not der:
        raise ProbeError(f"{host}:{port} delivered no certificate")
    return parse_notafter_der(der)


def parse_notafter_der(der: bytes) -> dt.datetime:
    """Reads notAfter from DER bytes -- via openssl, with a stdlib fallback."""
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-noout", "-enddate"],
            input=der, capture_output=True, timeout=20, check=False,
        )
        text = proc.stdout.decode("ascii", "replace").strip()
        if proc.returncode == 0 and text.startswith("notAfter="):
            return parse_openssl_date(text.split("=", 1)[1].strip())
    except (OSError, subprocess.SubprocessError):
        pass  # Fall through below -- openssl missing or behaving unexpectedly.

    raise ProbeError("could not read notAfter (openssl returned nothing)")


def parse_openssl_date(value: str) -> dt.datetime:
    """'Nov 16 16:46:17 2026 GMT' -> timezone-aware datetime (UTC).

    🔴 Deliberately NO strptime with %b: that is locale-dependent. Under a
    German locale, 'Nov 16 ...' would fail depending on the month and the
    watchdog would die with an exception -- loud, but unnecessarily so.
    """
    months = {m: i for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
    m = re.match(
        r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})\s*(GMT|UTC)?$",
        value.strip())
    if not m or m.group(1) not in months:
        raise ProbeError(f"unexpected date format from openssl: {value!r}")
    mon, day, hh, mm, ss, year = (
        months[m.group(1)], int(m.group(2)), int(m.group(3)),
        int(m.group(4)), int(m.group(5)), int(m.group(6)))
    return dt.datetime(year, mon, day, hh, mm, ss, tzinfo=dt.timezone.utc)


def days_left(not_after: dt.datetime, now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(dt.timezone.utc)
    return (not_after - now).total_seconds() / 86400.0


# --- Cooldown state ----------------------------------------------------------


def stage_of(days: float) -> int:
    """Smallest escalation step already undershot."""
    for step in ESCALATION_STEPS:
        if days < step:
            continue
        return step
    return -1  # already expired


def should_report(days: float, now_epoch: float) -> bool:
    """True if it should report now (respecting the cooldown).

    An ESCALATION (new, lower escalation step) always breaks through the
    cooldown -- otherwise a 24h window would swallow the transition from
    'still 3 days' to 'expired'.
    """
    stage = stage_of(days)
    if not STATE.exists():
        return True
    try:
        raw = STATE.read_text(encoding="utf-8").split()
        last_epoch, last_stage = float(raw[0]), int(raw[1])
    except (ValueError, OSError, IndexError):
        return True  # unreadable state -> better to report once too often
    if stage < last_stage:
        return True  # situation has gotten worse
    return (now_epoch - last_epoch) >= COOLDOWN_H * 3600


def remember(days: float, now_epoch: float) -> None:
    VAR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(f"{now_epoch:.0f} {stage_of(days)}\n", encoding="utf-8")


# --- Main run ------------------------------------------------------------


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    try:
        not_after = fetch_live_notafter(HOST, PORT, CONNECT_TIMEOUT)
    except ProbeError as exc:
        # 🔴 Fail-LOUD, not fail-silent: a failed measurement is not
        # 'all good'. exit != 0 triggers the scheduler's error alarm.
        print(f"TLS expiry monitor {HOST}:{PORT} -- MEASUREMENT FAILED: {exc}",
              file=sys.stderr)
        return 1

    days = days_left(not_after, now)
    if days >= WARN_DAYS:
        return 0  # silent -- the normal case

    if not should_report(days, now.timestamp()):
        return 0  # already reported, situation unchanged

    remember(days, now.timestamp())
    local = not_after.astimezone()
    if days < 0:
        headline = f"🔴 TLS certificate {HOST} has been EXPIRED for {abs(days):.1f} days"
    else:
        headline = f"⚠️ TLS certificate {HOST} expires in {days:.1f} days"
    print(headline)
    print(f"   Expires: {local:%Y-%m-%d %H:%M %Z} (delivered at port {PORT})")
    print(f"   Threshold: {WARN_DAYS} days.")
    print("   Context: the ACME path is broken (port 80 closed from outside),")
    print("   snap.certbot.renew.timer has been failing silently on every run since.")
    print("   Decision template: kanban card t_b8b9a6a7.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
