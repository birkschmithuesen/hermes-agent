#!/usr/bin/env python3
"""Ablaufmonitor fuer das LIVE ausgelieferte TLS-Zertifikat.

Zweck (Karte t_b8b9a6a7, Abnahmepunkt 5): Der ACME-Weg fuer
office.artesmobiles.art ist defekt (Port 80 von aussen dicht). Das aktuelle
Zertifikat laeuft am 2026-11-16 ab; snap.certbot.renew.timer wird ab ca.
2026-10-17 zweimal taeglich scheitern -- still, weil niemand die Ausgabe liest.
Dieser Waechter macht dieses Scheitern sichtbar, BEVOR der Dienst ausfaellt.

LLM-frei (`no_agent=True`): reine Stdlib, das Skript IST der Job.
Zustellsemantik, auf der das Ganze beruht:
  leere Ausgabe   -> stumm (Normalfall, fast immer)
  Text            -> wird woertlich zugestellt
  exit != 0       -> Fehleralarm (ein kaputter Waechter darf nicht still sein)

🔴 Gemessen wird die LIVE ausgelieferte Kette am Port, NICHT die Datei unter
/etc/letsencrypt/live/. Ein erneuertes Zertifikat, das der Deploy-Hook nicht in
den Container gebracht hat, wuerde auf der Platte frisch aussehen und am Port
trotzdem ablaufen -- genau der Fehlermodus, der hier zaehlt. (Der Deploy-Hook
war nachweislich noch nie gelaufen, siehe Skill tls-certificate-troubleshooting.)
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

# --- Konfiguration -----------------------------------------------------------

HOST = os.environ.get("CERT_WATCH_HOST", "office.artesmobiles.art")
PORT = int(os.environ.get("CERT_WATCH_PORT", "443"))

# Schwelle laut Abnahmepunkt 5: unter 21 Tagen melden.
# Bewusst > certbots eigenem renew_before_expiry = 30 Tage MINUS Sicherheits-
# abstand: certbot versucht ab Tag 30 zu erneuern. Meldet dieser Waechter erst
# bei 21, sind bereits ~9 Tage Erneuerungsversuche stillschweigend gescheitert
# -- das ist genau das Signal, das wir hoeren wollen, und es bleiben noch drei
# Wochen zum Handeln.
WARN_DAYS = int(os.environ.get("CERT_WATCH_WARN_DAYS", "21"))

# Cooldown: nicht taeglich dieselbe Warnung. Aber KEIN endgueltiges Verstummen
# -- die Lage kann sich verschlechtern (Skill llm-free-watchdog-jobs: das
# Abbruchkriterium gehoert an den Ausgang, nicht ans Melden). Wird die Lage
# dringender (naechste Eskalationsstufe erreicht), meldet er sofort erneut.
COOLDOWN_H = float(os.environ.get("CERT_WATCH_COOLDOWN_H", "24"))

# Eskalationsstufen in Tagen Restlaufzeit. Sinkt die Restlaufzeit unter eine
# neue Stufe, wird der Cooldown ignoriert und sofort gemeldet.
ESCALATION_STEPS = (21, 14, 7, 3, 1, 0)

CONNECT_TIMEOUT = float(os.environ.get("CERT_WATCH_TIMEOUT", "15"))

VAR = Path(os.environ.get(
    "CERT_WATCH_VAR",
    Path.home() / ".hermes" / "profiles" / "birk" / "var" / "cert-expiry-watch",
))
STATE = VAR / "last_alert.txt"

# --- Messung -----------------------------------------------------------------


class ProbeError(RuntimeError):
    """Die Messung selbst ist gescheitert -- das ist NIE 'alles gut'."""


def fetch_live_notafter(host: str, port: int, timeout: float) -> dt.datetime:
    """Holt notAfter aus dem am Port ausgelieferten Leaf-Zertifikat.

    Wichtig: ein normaler ssl-Handshake mit Verifikation liefert das geparste
    Zertifikat nur bei Erfolg. Wir wollen aber auch dann noch ein Ablaufdatum
    lesen koennen, wenn die Verifikation gerade WEGEN Ablaufs scheitert --
    sonst schweigt der Waechter genau im Ernstfall. Deshalb wird das DER
    unverifiziert geholt und separat geparst.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        raise ProbeError(f"Verbindung zu {host}:{port} fehlgeschlagen: {exc}") from exc

    if not der:
        raise ProbeError(f"{host}:{port} lieferte kein Zertifikat")
    return parse_notafter_der(der)


def parse_notafter_der(der: bytes) -> dt.datetime:
    """Liest notAfter aus DER-Bytes -- ueber openssl, mit Stdlib-Rueckfall."""
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-noout", "-enddate"],
            input=der, capture_output=True, timeout=20, check=False,
        )
        text = proc.stdout.decode("ascii", "replace").strip()
        if proc.returncode == 0 and text.startswith("notAfter="):
            return parse_openssl_date(text.split("=", 1)[1].strip())
    except (OSError, subprocess.SubprocessError):
        pass  # Rueckfall unten -- openssl fehlt oder verhaelt sich unerwartet.

    raise ProbeError("notAfter konnte nicht gelesen werden (openssl lieferte nichts)")


def parse_openssl_date(value: str) -> dt.datetime:
    """'Nov 16 16:46:17 2026 GMT' -> timezone-aware datetime (UTC).

    🔴 Bewusst KEIN strptime mit %b: das ist locale-abhaengig. Unter einer
    deutschen Locale wuerde 'Nov 16 ...' je nach Monat scheitern und der
    Waechter mit Exception sterben -- was zwar laut, aber unnoetig ist.
    """
    months = {m: i for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
    m = re.match(
        r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})\s*(GMT|UTC)?$",
        value.strip())
    if not m or m.group(1) not in months:
        raise ProbeError(f"Unerwartetes Datumsformat von openssl: {value!r}")
    mon, day, hh, mm, ss, year = (
        months[m.group(1)], int(m.group(2)), int(m.group(3)),
        int(m.group(4)), int(m.group(5)), int(m.group(6)))
    return dt.datetime(year, mon, day, hh, mm, ss, tzinfo=dt.timezone.utc)


def days_left(not_after: dt.datetime, now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(dt.timezone.utc)
    return (not_after - now).total_seconds() / 86400.0


# --- Cooldown-Zustand --------------------------------------------------------


def stage_of(days: float) -> int:
    """Kleinste Eskalationsstufe, die bereits unterschritten ist."""
    for step in ESCALATION_STEPS:
        if days < step:
            continue
        return step
    return -1  # bereits abgelaufen


def should_report(days: float, now_epoch: float) -> bool:
    """True, wenn jetzt gemeldet werden soll (Cooldown beachtet).

    Eine VERSCHAERFUNG (neue, niedrigere Eskalationsstufe) durchbricht den
    Cooldown immer -- sonst verschluckt ein 24-h-Fenster den Uebergang von
    'noch 3 Tage' auf 'abgelaufen'.
    """
    stage = stage_of(days)
    if not STATE.exists():
        return True
    try:
        raw = STATE.read_text(encoding="utf-8").split()
        last_epoch, last_stage = float(raw[0]), int(raw[1])
    except (ValueError, OSError, IndexError):
        return True  # unlesbarer Zustand -> lieber einmal zu viel melden
    if stage < last_stage:
        return True  # Lage hat sich verschaerft
    return (now_epoch - last_epoch) >= COOLDOWN_H * 3600


def remember(days: float, now_epoch: float) -> None:
    VAR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(f"{now_epoch:.0f} {stage_of(days)}\n", encoding="utf-8")


# --- Hauptlauf ---------------------------------------------------------------


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    try:
        not_after = fetch_live_notafter(HOST, PORT, CONNECT_TIMEOUT)
    except ProbeError as exc:
        # 🔴 Fail-LOUD, nicht fail-silent: eine gescheiterte Messung ist kein
        # 'alles gut'. exit != 0 loest den Fehleralarm des Schedulers aus.
        print(f"TLS-Ablaufmonitor {HOST}:{PORT} -- MESSUNG GESCHEITERT: {exc}",
              file=sys.stderr)
        return 1

    days = days_left(not_after, now)
    if days >= WARN_DAYS:
        return 0  # stumm -- der Normalfall

    if not should_report(days, now.timestamp()):
        return 0  # bereits gemeldet, Lage unveraendert

    remember(days, now.timestamp())
    local = not_after.astimezone()
    if days < 0:
        headline = f"🔴 TLS-Zertifikat {HOST} ist seit {abs(days):.1f} Tagen ABGELAUFEN"
    else:
        headline = f"⚠️ TLS-Zertifikat {HOST} laeuft in {days:.1f} Tagen ab"
    print(headline)
    print(f"   Ablauf: {local:%Y-%m-%d %H:%M %Z} (am Port {PORT} ausgeliefert)")
    print(f"   Schwelle: {WARN_DAYS} Tage.")
    print("   Kontext: der ACME-Weg ist defekt (Port 80 von aussen dicht),")
    print("   snap.certbot.renew.timer scheitert seither bei jedem Lauf still.")
    print("   Entscheidungsvorlage: Kanban-Karte t_b8b9a6a7.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
