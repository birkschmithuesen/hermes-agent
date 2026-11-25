"""Generated service behavior for the opt-in systemd watchdog."""

from __future__ import annotations

from gateway.config import GatewayConfig
from hermes_cli import gateway as gateway_cli




def test_positive_watchdog_config_generates_notify_unit(monkeypatch):
    monkeypatch.setattr(
        gateway_cli,
        "load_gateway_config",
        lambda: GatewayConfig.from_dict({"systemd_watchdog_seconds": 120}),
        raising=False,
    )

    unit = gateway_cli.generate_systemd_unit(system=False)

    assert "Type=notify" in unit
    assert "NotifyAccess=main" in unit
    assert "WatchdogSec=120s" in unit






def test_watchdog_seconds_scoped_to_default_home_not_a_named_profile_dir(tmp_path):
    """Under ``gateway.multiplex_profiles``, one systemd unit serves every profile from the
    DEFAULT home's own config.yaml; a value set only in a named profile's ``profiles/<name>/
    config.yaml`` (profiles are independent islands, no config inheritance) must never leak into
    the unit the default home generates — that scope confusion is exactly how a watchdog setting
    written to the wrong file silently never took effect."""
    default_home = tmp_path / "home"
    default_home.mkdir()
    (default_home / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: true\n  systemd_watchdog_seconds: 300\n",
        encoding="utf-8",
    )
    named_profile_dir = default_home / "profiles" / "birk"
    named_profile_dir.mkdir(parents=True)
    (named_profile_dir / "config.yaml").write_text(
        "gateway:\n  systemd_watchdog_seconds: 999\n",
        encoding="utf-8",
    )

    assert gateway_cli._systemd_watchdog_seconds(default_home) == 300


def test_system_unit_reads_watchdog_from_target_home(tmp_path, monkeypatch):
    caller_home = tmp_path / "caller"
    target_home = tmp_path / "target"
    caller_home.mkdir()
    target_home.mkdir()
    (caller_home / "config.yaml").write_text(
        "gateway:\n  systemd_watchdog_seconds: 0\n",
        encoding="utf-8",
    )
    (target_home / "config.yaml").write_text(
        "gateway:\n  systemd_watchdog_seconds: 75\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(caller_home))
    monkeypatch.setattr(
        gateway_cli,
        "_system_service_identity",
        lambda _user: ("service", "service", str(tmp_path / "account"), 1001),
    )
    monkeypatch.setattr(
        gateway_cli,
        "_hermes_home_for_target_user",
        lambda _home: str(target_home),
    )

    unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="service")

    assert "Type=notify" in unit
    assert "WatchdogSec=75s" in unit


