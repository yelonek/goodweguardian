"""Alarmy anomalii: detektory + debounce + ntfy."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alert_store import AlertFileState, apply_firings, process_cycle_alerts
from anomaly_alerts import (
    RULE_EV_EXPENSIVE_FAST,
    RULE_GRID_IMPORT_PV,
    RULE_PLAN_DISCHARGE_MISS,
    RULE_PLANNER_SILENT_FAIL,
    AlertFiring,
    AnomalySnapshot,
    charging_kw_from_twc,
    evaluate,
)
from guardian_settings import GuardianSettings
from tariff_g12 import G12TariffConfig
import ntfy_notify
import guardian_config as gc


DAY_TARIFF = G12TariffConfig(
    distribution_day_pln_per_kwh=0.40,
    distribution_night_pln_per_kwh=0.10,
    energy_day_pln_per_kwh=0.70,
    energy_night_pln_per_kwh=0.30,
)

SETTINGS = GuardianSettings()


def _ids(firings: list[AlertFiring]) -> set[str]:
    return {f.rule_id for f in firings}


def _snap(**kw: object) -> AnomalySnapshot:
    base: dict[str, object] = {
        "local_hour": 11,
        "local_minute": 15,
        "grid_w": -2000.0,
        "pv_w": 3000.0,
        "consumption_w": 5000.0,
        "soc_pct": 60.0,
        "battery_w": 0.0,
        "remaining_kwh": -0.4,
        "time_to_end_s": 2700.0,
        "charging_kw": 9.2,
        "exec_mode": "neutral",
        "planner_execution_enabled": True,
        "policy_missing": False,
        "control_enabled": True,
        "watchdog_reason": "flappy_buffer_build",
    }
    base.update(kw)
    return AnomalySnapshot.model_validate(base)


def test_charging_kw_from_twc_one_minute() -> None:
    # 9.2 kW × 1 min = 9.2/60 kWh
    assert charging_kw_from_twc(10.0, 10.0 + 9.2 / 60.0, 60.0) == pytest.approx(9.2)
    assert charging_kw_from_twc(None, 10.0, 60.0) is None
    assert charging_kw_from_twc(10.0, 10.1, 400.0) is None


def test_ev_11kw_at_11_fires() -> None:
    firings = evaluate(_snap(), settings=SETTINGS, tariff=DAY_TARIFF)
    assert RULE_EV_EXPENSIVE_FAST in _ids(firings)
    ev = next(f for f in firings if f.rule_id == RULE_EV_EXPENSIVE_FAST)
    assert "9.2" in ev.title


def test_ev_11kw_at_23_silent() -> None:
    firings = evaluate(
        _snap(local_hour=23, charging_kw=11.0),
        settings=SETTINGS,
        tariff=DAY_TARIFF,
    )
    assert RULE_EV_EXPENSIVE_FAST not in _ids(firings)


def test_grid_import_pv_expensive_fires() -> None:
    firings = evaluate(
        _snap(charging_kw=0.0, grid_w=-2000.0, pv_w=3000.0, battery_w=0.0),
        settings=SETTINGS,
        tariff=DAY_TARIFF,
    )
    assert RULE_GRID_IMPORT_PV in _ids(firings)


def test_charge_grid_import_silent() -> None:
    firings = evaluate(
        _snap(
            charging_kw=0.0,
            exec_mode="charge_grid",
            grid_w=-3000.0,
            pv_w=3000.0,
        ),
        settings=SETTINGS,
        tariff=DAY_TARIFF,
    )
    assert RULE_GRID_IMPORT_PV not in _ids(firings)
    assert RULE_EV_EXPENSIVE_FAST not in _ids(firings)


def test_plan_discharge_miss_after_minute_15() -> None:
    # target +2.0, 15 min → expected 0.5; remaining 0.0 → lag 0.5 < 0.8, still miss if lag raised
    # minute 15, time_to_end 2700s → elapsed 900s = 0.25h → expected 2*0.25=0.5
    # remaining -0.4 → lag 0.9 ≥ 0.8, battery idle
    firings = evaluate(
        _snap(
            charging_kw=0.0,
            exec_mode="export_profit",
            plan_target_net_kwh=2.0,
            remaining_kwh=-0.4,
            local_minute=15,
            time_to_end_s=2700.0,
            battery_w=20.0,
            grid_w=100.0,
            pv_w=0.0,
        ),
        settings=SETTINGS,
        tariff=DAY_TARIFF,
    )
    assert RULE_PLAN_DISCHARGE_MISS in _ids(firings)


def test_plan_discharge_miss_skipped_when_battery_discharges() -> None:
    firings = evaluate(
        _snap(
            charging_kw=0.0,
            exec_mode="export_profit",
            plan_target_net_kwh=2.0,
            remaining_kwh=-0.4,
            battery_w=2000.0,
            grid_w=100.0,
            pv_w=0.0,
        ),
        settings=SETTINGS,
        tariff=DAY_TARIFF,
    )
    assert RULE_PLAN_DISCHARGE_MISS not in _ids(firings)


def test_plan_discharge_miss_skipped_soc_hold() -> None:
    firings = evaluate(
        _snap(
            charging_kw=0.0,
            exec_mode="export_profit",
            plan_target_net_kwh=2.0,
            remaining_kwh=-0.4,
            watchdog_reason="export_profit_soc_floor",
        ),
        settings=SETTINGS,
        tariff=DAY_TARIFF,
    )
    assert RULE_PLAN_DISCHARGE_MISS not in _ids(firings)


def test_planner_silent_fail_no_policy() -> None:
    firings = evaluate(
        _snap(charging_kw=0.0, policy_missing=True, exec_mode=None),
        settings=SETTINGS,
        tariff=DAY_TARIFF,
    )
    assert RULE_PLANNER_SILENT_FAIL in _ids(firings)


def test_planner_silent_fail_control_off() -> None:
    firings = evaluate(
        _snap(
            charging_kw=0.0,
            control_enabled=False,
            exec_mode="export_profit",
            plan_target_net_kwh=1.0,
        ),
        settings=SETTINGS,
        tariff=DAY_TARIFF,
    )
    assert RULE_PLANNER_SILENT_FAIL in _ids(firings)


def test_debounce_two_minutes_no_push() -> None:
    firing = AlertFiring(rule_id="ev_expensive_fast", title="t", body="b")
    state = AlertFileState()
    now = datetime(2026, 9, 8, 11, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    p1 = apply_firings(
        state, [firing], now=now, debounce_min=3, cooldown_min=30, clear_min=2
    )
    p2 = apply_firings(
        state,
        [firing],
        now=now + timedelta(minutes=1),
        debounce_min=3,
        cooldown_min=30,
        clear_min=2,
    )
    assert p1 == []
    assert p2 == []
    p3 = apply_firings(
        state,
        [firing],
        now=now + timedelta(minutes=2),
        debounce_min=3,
        cooldown_min=30,
        clear_min=2,
    )
    assert len(p3) == 1
    p4 = apply_firings(
        state,
        [firing],
        now=now + timedelta(minutes=3),
        debounce_min=3,
        cooldown_min=30,
        clear_min=2,
    )
    assert p4 == []  # cooldown


def test_ntfy_posts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(gc, "NTFY_URL", "https://ntfy.example/guardian-alerts")
    monkeypatch.setattr(gc, "NTFY_TOKEN", "tk_test")
    monkeypatch.setattr(ntfy_notify, "_EMPTY_URL_WARNED", True)
    monkeypatch.setattr("httpx.post", fake_post)
    ok = ntfy_notify.send_ntfy("Tytuł", "treść")
    assert ok is True
    assert len(calls) == 1
    assert calls[0]["url"] == "https://ntfy.example"
    assert calls[0]["json"]["topic"] == "guardian-alerts"
    assert calls[0]["json"]["title"] == "Tytuł"
    assert calls[0]["headers"]["Authorization"] == "Bearer tk_test"


def test_process_cycle_ntfy_once_then_cooldown(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    path = tmp_path / "alerts.json"
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "alert_store.send_ntfy", lambda title, body: sent.append((title, body)) or True
    )
    monkeypatch.setattr(
        "alert_store.get_settings",
        lambda: SETTINGS.model_copy(update={"anomaly_debounce_min": 1, "anomaly_cooldown_min": 30}),
    )
    tz = ZoneInfo("Europe/Warsaw")
    t0 = datetime(2026, 9, 8, 11, 10, tzinfo=tz)
    kwargs = dict(
        grid_w=-8000.0,
        pv_w=0.0,
        consumption_w=9000.0,
        soc_pct=50.0,
        battery_w=0.0,
        remaining_kwh=-1.0,
        time_to_end_s=3000.0,
        plan_target_net_kwh=None,
        exec_mode="neutral",
        planner_execution_enabled=True,
        policy_missing=False,
        control_enabled=True,
        watchdog_reason="x",
        path=path,
        notify=True,
    )
    # first sample seeds TWC
    process_cycle_alerts(now=t0, E_twc_kwh=100.0, **kwargs)
    # 11 kW for 60s
    process_cycle_alerts(
        now=t0 + timedelta(seconds=60), E_twc_kwh=100.0 + 11.0 / 60.0, **kwargs
    )
    assert len(sent) == 1
    process_cycle_alerts(
        now=t0 + timedelta(seconds=120), E_twc_kwh=100.0 + 22.0 / 60.0, **kwargs
    )
    assert len(sent) == 1
