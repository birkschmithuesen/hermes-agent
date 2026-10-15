#!/usr/bin/env python3
"""Isolated verification of cert_expiry_watch.py (card t_b8b9a6a7, point 5).

Throwaway harness per skill llm-free-watchdog-jobs, invariant 7:
  - redirect state (VAR/STATE) to a tempfile.mkdtemp()
  - mock the network side effects
  - run through BOTH extremes and the error case
  - check that the alarm texts appear verbatim

Mutation test (acceptance point 5): against an artificially short remaining
lifetime it MUST trigger, in the normal case it MUST stay silent. Both are
measured here, not asserted.

Run:  python3 verify_cert_expiry_watch.py
Exit 0 = all cases as expected.
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
    """Loads the script fresh with the state directory redirected."""
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
    """Runs main() with a mocked measurement and checks output + return code."""
    tmp = vardir or Path(tempfile.mkdtemp(prefix="hermes-verify-cert-"))
    mod = module or load_module(tmp)

    def fake_fetch(host, port, timeout):
        if probe_raises:
            raise mod.ProbeError("simulated connection error")
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
        detail.append(f"output expected={expect_output}, was={combined.strip()!r}")
    if rc != expect_rc:
        ok = False
        detail.append(f"rc expected={expect_rc}, was={rc}")
    for needle in must_contain:
        if needle not in combined:
            ok = False
            detail.append(f"missing from text: {needle!r}")

    results.append((name, ok, "; ".join(detail) or combined.strip()[:150]))
    return mod, tmp


def main() -> int:
    print("=" * 72)
    print("Mutation test cert_expiry_watch.py")
    print("=" * 72)

    # --- 1. NORMAL CASE: plenty of remaining lifetime -> MUST stay silent ---
    run_case("Normal case (70 days left) -> SILENT",
             days_offset=70, expect_output=False, expect_rc=0)

    # --- 2. MUTATION: short remaining lifetime -> MUST trigger -------------
    run_case("Mutation (5 days left) -> ALARM",
             days_offset=5, expect_output=True, expect_rc=0,
             must_contain=("expires in", "office.artesmobiles.art"))

    # --- 3. Exactly AT the threshold (21) -> stays silent (>= WARN_DAYS) ----
    run_case("Edge case exactly 21.5 days -> SILENT",
             days_offset=21.5, expect_output=False, expect_rc=0)

    # --- 4. Just UNDER the threshold -> reports -----------------------------
    run_case("Edge case 20.5 days -> ALARM",
             days_offset=20.5, expect_output=True, expect_rc=0,
             must_contain=("expires in",))

    # --- 5. Already expired -> different wording ----------------------------
    run_case("Expired (-2 days) -> ALARM 'EXPIRED'",
             days_offset=-2, expect_output=True, expect_rc=0,
             must_contain=("EXPIRED",))

    # --- 6. MISSING SIGNAL: measurement fails -> fail LOUD, exit 1 ----------
    #     This is the most important line: a failed measurement must NEVER
    #     pass as 'all good' (fail-closed).
    run_case("Measurement failed -> exit 1, loud",
             days_offset=None, probe_raises=True,
             expect_output=True, expect_rc=1,
             must_contain=("MEASUREMENT FAILED",))

    # --- 7. COOLDOWN: second run at the same situation -> silent ------------
    shared = Path(tempfile.mkdtemp(prefix="hermes-verify-cert-cd-"))
    mod, _ = run_case("Cooldown 1st run (10 days) -> ALARM",
                      days_offset=10, expect_output=True, expect_rc=0,
                      vardir=shared)
    run_case("Cooldown 2nd run (10 days, same step) -> SILENT",
             days_offset=10, expect_output=False, expect_rc=0,
             vardir=shared, module=mod)

    # --- 8. ESCALATION breaks through the cooldown --------------------------
    #     Otherwise a 24h window would swallow the transition from 'still
    #     3 days' to 'expired'. Same state as case 7, but worse situation.
    run_case("Escalation (10 -> 2 days) breaks cooldown -> ALARM",
             days_offset=2, expect_output=True, expect_rc=0,
             vardir=shared, module=mod, must_contain=("expires in",))

    # --- 9. Date parser is locale-proof --------------------------------------
    parsed = mod.parse_openssl_date("Nov 16 16:46:17 2026 GMT")
    ok9 = (parsed.year, parsed.month, parsed.day, parsed.hour) == (2026, 11, 16, 16)
    results.append(("Date parser 'Nov 16 ... 2026 GMT'", ok9, str(parsed)))

    # --- 10. Garbage format is recognized as an error, not silently --------
    try:
        mod.parse_openssl_date("16.11.2026 something")
        results.append(("Garbage date -> ProbeError", False, "no exception"))
    except mod.ProbeError as exc:
        results.append(("Garbage date -> ProbeError", True, str(exc)[:80]))

    # --- Report --------------------------------------------------------------
    print()
    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, detail in results:
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {name:<{width}}  {detail[:90]}")
        failed += 0 if ok else 1

    print()
    print(f"{len(results) - failed}/{len(results)} cases as expected.")

    # Harness cleans up after itself -- the real state stays untouched.
    for p in Path(tempfile.gettempdir()).glob("hermes-verify-cert-*"):
        shutil.rmtree(p, ignore_errors=True)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
