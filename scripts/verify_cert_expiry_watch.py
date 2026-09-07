#!/usr/bin/env python3
"""Isolierte Verifikation von cert_expiry_watch.py (Karte t_b8b9a6a7, Punkt 5).

Wegwerf-Harness nach Skill llm-free-watchdog-jobs, Invariante 7:
  - Zustand (VAR/STATE) auf ein tempfile.mkdtemp() umbiegen
  - die Netz-Seiteneffekte mocken
  - BEIDE Extreme und den Fehlerfall durchspielen
  - pruefen, dass die Alarmtexte woertlich erscheinen

Mutationsprobe (Abnahmepunkt 5): gegen einen kuenstlich auf kurze Restlaufzeit
gesetzten Vergleichswert MUSS er ausloesen, im Normalfall MUSS er schweigen.
Beides wird hier gemessen, nicht behauptet.

Aufruf:  python3 verify_cert_expiry_watch.py
Exit 0 = alle Faelle wie erwartet.
"""
from __future__ import annotations

import datetime as dt
import io
import importlib.util
import shutil
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "cert_expiry_watch.py"

results: list[tuple[str, bool, str]] = []


def load_module(vardir: Path):
    """Laedt das Skript frisch mit umgebogenem Zustandsverzeichnis."""
    import os
    os.environ["CERT_WATCH_VAR"] = str(vardir)
    spec = importlib.util.spec_from_file_location("cew_under_test", TARGET)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_case(name: str, days_offset: float | None, *, probe_raises: bool = False,
             expect_output: bool, expect_rc: int, must_contain: tuple[str, ...] = (),
             vardir: Path | None = None, module=None):
    """Faehrt main() mit gemockter Messung und prueft Ausgabe + Rueckgabecode."""
    tmp = vardir or Path(tempfile.mkdtemp(prefix="hermes-verify-cert-"))
    mod = module or load_module(tmp)

    def fake_fetch(host, port, timeout):
        if probe_raises:
            raise mod.ProbeError("simulierter Verbindungsfehler")
        assert days_offset is not None
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days_offset)

    mod.fetch_live_notafter = fake_fetch  # type: ignore[assignment]

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod.main()
    combined = out.getvalue() + err.getvalue()

    ok = True
    detail = []
    if (combined.strip() != "") != expect_output:
        ok = False
        detail.append(f"Ausgabe erwartet={expect_output}, war={combined.strip()!r}")
    if rc != expect_rc:
        ok = False
        detail.append(f"rc erwartet={expect_rc}, war={rc}")
    for needle in must_contain:
        if needle not in combined:
            ok = False
            detail.append(f"fehlt im Text: {needle!r}")

    results.append((name, ok, "; ".join(detail) or combined.strip()[:150]))
    return mod, tmp


def main() -> int:
    print("=" * 72)
    print("Mutationsprobe cert_expiry_watch.py")
    print("=" * 72)

    # --- 1. NORMALFALL: viel Restlaufzeit -> MUSS schweigen -----------------
    run_case("Normalfall (70 Tage Rest) -> STUMM",
             days_offset=70, expect_output=False, expect_rc=0)

    # --- 2. MUTATION: kurze Restlaufzeit -> MUSS ausloesen ------------------
    run_case("Mutation (5 Tage Rest) -> ALARM",
             days_offset=5, expect_output=True, expect_rc=0,
             must_contain=("laeuft in", "office.artesmobiles.art"))

    # --- 3. Genau AUF der Schwelle (21) -> schweigt (>= WARN_DAYS) ----------
    run_case("Grenzfall exakt 21.5 Tage -> STUMM",
             days_offset=21.5, expect_output=False, expect_rc=0)

    # --- 4. Knapp UNTER der Schwelle -> meldet -----------------------------
    run_case("Grenzfall 20.5 Tage -> ALARM",
             days_offset=20.5, expect_output=True, expect_rc=0,
             must_contain=("laeuft in",))

    # --- 5. Bereits abgelaufen -> anderer Wortlaut -------------------------
    run_case("Abgelaufen (-2 Tage) -> ALARM 'ABGELAUFEN'",
             days_offset=-2, expect_output=True, expect_rc=0,
             must_contain=("ABGELAUFEN",))

    # --- 6. FEHLENDES SIGNAL: Messung scheitert -> fail LOUD, exit 1 -------
    #     Das ist die wichtigste Zeile: eine gescheiterte Messung darf NIE
    #     als 'alles gut' durchgehen (fail-closed).
    run_case("Messung gescheitert -> exit 1, laut",
             days_offset=None, probe_raises=True,
             expect_output=True, expect_rc=1,
             must_contain=("MESSUNG GESCHEITERT",))

    # --- 7. COOLDOWN: zweiter Lauf bei gleicher Lage -> stumm --------------
    shared = Path(tempfile.mkdtemp(prefix="hermes-verify-cert-cd-"))
    mod, _ = run_case("Cooldown 1. Lauf (10 Tage) -> ALARM",
                      days_offset=10, expect_output=True, expect_rc=0,
                      vardir=shared)
    run_case("Cooldown 2. Lauf (10 Tage, gleiche Stufe) -> STUMM",
             days_offset=10, expect_output=False, expect_rc=0,
             vardir=shared, module=mod)

    # --- 8. ESKALATION durchbricht den Cooldown ----------------------------
    #     Sonst verschluckt ein 24-h-Fenster den Uebergang 'noch 3 Tage' ->
    #     'abgelaufen'. Gleicher Zustand wie Fall 7, aber verschaerfte Lage.
    run_case("Eskalation (10 -> 2 Tage) durchbricht Cooldown -> ALARM",
             days_offset=2, expect_output=True, expect_rc=0,
             vardir=shared, module=mod, must_contain=("laeuft in",))

    # --- 9. Datumsparser ist locale-fest -----------------------------------
    parsed = mod.parse_openssl_date("Nov 16 16:46:17 2026 GMT")
    ok9 = (parsed.year, parsed.month, parsed.day, parsed.hour) == (2026, 11, 16, 16)
    results.append(("Datumsparser 'Nov 16 ... 2026 GMT'", ok9, str(parsed)))

    # --- 10. Muellformat wird als Fehler erkannt, nicht stillschweigend ----
    try:
        mod.parse_openssl_date("16.11.2026 irgendwas")
        results.append(("Muell-Datum -> ProbeError", False, "keine Exception"))
    except mod.ProbeError as exc:
        results.append(("Muell-Datum -> ProbeError", True, str(exc)[:80]))

    # --- Bericht -----------------------------------------------------------
    print()
    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, detail in results:
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {name:<{width}}  {detail[:90]}")
        failed += 0 if ok else 1

    print()
    print(f"{len(results) - failed}/{len(results)} Faelle wie erwartet.")

    # Harness raeumt hinter sich auf -- der echte Zustand bleibt unangetastet.
    for p in Path(tempfile.gettempdir()).glob("hermes-verify-cert-*"):
        shutil.rmtree(p, ignore_errors=True)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
