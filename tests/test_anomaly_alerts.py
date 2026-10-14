"""Alarmy: bilans, tani eksport, egzekucja planu, zapowiedź, zbiorczy push."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alert_store import AlertFileState, RuleRuntime, apply_firings, process_cycle_alerts
from anomaly_alerts import (
    RULE_EV_OUTSIDE_CHEAP,
    RULE_EXPORT_BELOW_CHEAP,
    RULE_NET_NEGATIVE_EXPENSIVE,
    RULE_NET_NEGATIVE_MIDDAY,
    RULE_PLAN_DISCHARGE_MISS,
    RULE_PLAN_IMPORT,
    RULE_PLANNER_SILENT_FAIL,
    AlertFiring,
    AlertOutlook,
    AnomalySnapshot,
    PlanSlot,
    charging_kw_from_twc,
    day_is_armed,
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
CHEAP = 0.40

SETTINGS = GuardianSettings()
TZ = ZoneInfo("Europe/Warsaw")


def _ids(firings: list[AlertFiring]) -> set[str]:
    return {f.rule_id for f in firings}


def _snap(**kw: object) -> AnomalySnapshot:
    base: dict[str, object] = {
        "local_hour": 11,
        "local_minute": 15,
        "grid_w": 0.0,
        "pv_w": 3000.0,
        "consumption_w": 5000.0,
        "soc_pct": 40.0,
        "battery_w": 0.0,
        "remaining_kwh": 0.1,
        "time_to_end_s": 2700.0,
        "charging_kw": 4.4,
        "exec_mode": "neutral",
        "planner_execution_enabled": True,
        "policy_missing": False,
        "control_enabled": True,
        "watchdog_reason": "flappy_buffer_build",
        "plan_target_net_kwh": 0.0,
    }
    base.update(kw)
    return AnomalySnapshot.model_validate(base)


def _outlook(**kw: object) -> AlertOutlook:
    base: dict[str, object] = {
        "rce_pln_kwh": 0.02,
        "cheap_import_pln_kwh": CHEAP,
        "soc_full_pct": 99.0,
        "day_armed": False,
    }
    base.update(kw)
    return AlertOutlook.model_validate(base)


def _eval(snap: AnomalySnapshot, outlook: AlertOutlook | None = None) -> list[AlertFiring]:
    return evaluate(snap, outlook or _outlook(), settings=SETTINGS, tariff=DAY_TARIFF)


def test_charging_kw_from_twc_one_minute() -> None:
    assert charging_kw_from_twc(10.0, 10.0 + 9.2 / 60.0, 60.0) == pytest.approx(9.2)
    assert charging_kw_from_twc(None, 10.0, 60.0) is None
    assert charging_kw_from_twc(10.0, 10.1, 400.0) is None


def test_ev_power_in_day_hour_with_nonnegative_balance_is_silent() -> None:
    firings = _eval(_snap(charging_kw=4.4, remaining_kwh=0.04, grid_w=-200.0))
    assert RULE_NET_NEGATIVE_EXPENSIVE not in _ids(firings)
    assert RULE_EXPORT_BELOW_CHEAP not in _ids(firings)


def test_negative_balance_in_expensive_hour_without_abnormal_load_is_silent() -> None:
    """Sam minus rano (zwykły dom) nie alarmuje — szum 2026-10-13."""
    firings = _eval(
        _snap(remaining_kwh=-0.04, consumption_w=600.0, charging_kw=0.0)
    )
    assert RULE_NET_NEGATIVE_EXPENSIVE not in _ids(firings)


def test_morning_abnormal_ev_with_negative_balance_alerts() -> None:
    firings = _eval(
        _snap(remaining_kwh=-0.04, consumption_w=5000.0, charging_kw=4.4)
    )
    hit = next(f for f in firings if f.rule_id == RULE_NET_NEGATIVE_EXPENSIVE)
    assert hit.title.startswith("Duży pobór")
    assert hit.once_per_hour is True
    assert hit.remind is False
    assert "auto" in hit.body


def test_negative_balance_at_night_is_silent() -> None:
    firings = _eval(
        _snap(
            local_hour=23,
            remaining_kwh=-0.4,
            consumption_w=6000.0,
            charging_kw=0.0,
        )
    )
    assert RULE_NET_NEGATIVE_EXPENSIVE not in _ids(firings)
    assert RULE_NET_NEGATIVE_MIDDAY not in _ids(firings)


def test_midday_import_silent_without_abnormal_load() -> None:
    quiet = _eval(
        _snap(
            local_hour=14,
            remaining_kwh=-0.5,
            consumption_w=5000.0,
            charging_kw=4.4,
        ),
        _outlook(day_armed=True),
    )
    assert RULE_NET_NEGATIVE_MIDDAY not in _ids(quiet)
    assert RULE_NET_NEGATIVE_EXPENSIVE not in _ids(quiet)


def test_midday_heater_with_negative_balance_alerts() -> None:
    firings = _eval(
        _snap(
            local_hour=14,
            remaining_kwh=-0.5,
            consumption_w=4000.0,
            charging_kw=0.0,
        )
    )
    hit = next(f for f in firings if f.rule_id == RULE_NET_NEGATIVE_MIDDAY)
    assert "4.0 kW" in hit.body


def test_evening_plan_import_with_normal_load_is_silent() -> None:
    firings = _eval(
        _snap(local_hour=16, remaining_kwh=-0.2, consumption_w=600.0, charging_kw=0.0)
    )
    assert RULE_NET_NEGATIVE_EXPENSIVE not in _ids(firings)


def test_evening_heater_with_negative_balance_alerts() -> None:
    firings = _eval(
        _snap(local_hour=17, remaining_kwh=-0.3, consumption_w=3500.0, charging_kw=0.0)
    )
    hit = next(f for f in firings if f.rule_id == RULE_NET_NEGATIVE_EXPENSIVE)
    assert hit.title.startswith("Duży pobór")
    assert "3.5 kW" in hit.body


def test_evening_car_with_negative_balance_alerts() -> None:
    firings = _eval(
        _snap(local_hour=16, remaining_kwh=-0.4, consumption_w=800.0, charging_kw=7.2)
    )
    hit = next(f for f in firings if f.rule_id == RULE_NET_NEGATIVE_EXPENSIVE)
    assert "7.2 kW" in hit.body


def test_day_armed_by_charge_between_6_and_15_only() -> None:
    assert day_is_armed({9: 1.0}, {}) is True
    assert day_is_armed({}, {14: 0.5}) is True
    assert day_is_armed({5: 4.0}, {}) is False
    assert day_is_armed({}, {15: 3.0}) is False
    assert day_is_armed({22: 2.0}, {}) is False


def test_export_below_cheap_when_balance_exceeds_buffer() -> None:
    firings = _eval(
        _snap(exec_mode="neutral", soc_pct=33.0, remaining_kwh=0.50, grid_w=800.0)
    )
    hit = next(f for f in firings if f.rule_id == RULE_EXPORT_BELOW_CHEAP)
    assert hit.once_per_hour is True
    assert hit.remind is False
    assert "0.50" in hit.body


def test_export_inside_start_of_hour_buffer_is_silent() -> None:
    # 14:01: chwilowy eksport, bilans godziny jeszcze w buforze.
    firings = _eval(
        _snap(
            local_hour=14,
            local_minute=1,
            grid_w=820.0,
            exec_mode="neutral",
            soc_pct=46.0,
            remaining_kwh=-0.01,
        )
    )
    assert RULE_EXPORT_BELOW_CHEAP not in _ids(firings)


def test_export_below_cheap_full_battery_is_once_per_hour() -> None:
    firings = _eval(
        _snap(grid_w=800.0, soc_pct=99.0, exec_mode="neutral", remaining_kwh=0.50)
    )
    hit = next(f for f in firings if f.rule_id == RULE_EXPORT_BELOW_CHEAP)
    assert hit.once_per_hour is True
    assert hit.remind is False


def test_export_profit_does_not_use_the_price_check() -> None:
    firings = _eval(
        _snap(
            grid_w=2000.0,
            exec_mode="export_profit",
            battery_w=2000.0,
            remaining_kwh=1.0,
            plan_target_net_kwh=1.0,
        )
    )
    assert RULE_EXPORT_BELOW_CHEAP not in _ids(firings)


def test_high_rce_export_is_silent() -> None:
    firings = _eval(
        _snap(remaining_kwh=0.80),
        _outlook(rce_pln_kwh=0.80),
    )
    assert RULE_EXPORT_BELOW_CHEAP not in _ids(firings)


def test_outlook_rce_is_export_settlement_not_raw_market(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Godzina 11 z 2026-10-08: panel 0,6144 zł, alert brał surowe 0,50 zł."""
    import alert_store
    import energy_pricing as ep

    hourly = [0.5] * 24
    hourly[11] = 0.499535
    (tmp_path / "proxy_rce_2026-10-08.json").write_text(
        json.dumps({"hourly_rce_pln_per_kwh": hourly}),
        encoding="utf-8",
    )
    monkeypatch.setattr(alert_store, "PRICING_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        ep, "get_settings", lambda: SimpleNamespace(rce_export_multiplier=1.23)
    )
    monkeypatch.setattr(alert_store, "hourly_ev_kwh_from_telemetry", lambda _day: {})
    monkeypatch.setattr(alert_store, "load_latest_plan", lambda: None)

    outlook = alert_store.load_alert_outlook(
        datetime(2026, 10, 8, 11, 20, tzinfo=TZ),
        pace_remaining_kwh=None,
        pace_dt_s=None,
    )
    assert outlook.rce_pln_kwh == pytest.approx(0.61442805)


def test_live_streak_resets_when_the_hour_changes() -> None:
    firing = AlertFiring(rule_id=RULE_EXPORT_BELOW_CHEAP, title="Eksport", body="ponad bufor")
    state = AlertFileState()
    start = datetime(2026, 9, 27, 13, 50, tzinfo=TZ)
    pushed = []
    for minute in range(6):
        pushed.extend(
            apply_firings(
                state,
                [firing],
                now=start + timedelta(minutes=minute),
                debounce_min=5,
                cooldown_min=5,
                clear_min=2,
            )
        )
    assert len(pushed) == 1
    assert state.rules[RULE_EXPORT_BELOW_CHEAP].active is True
    at_1401 = apply_firings(
        state,
        [firing],
        now=datetime(2026, 9, 27, 14, 1, tzinfo=TZ),
        debounce_min=5,
        cooldown_min=5,
        clear_min=2,
    )
    assert at_1401 == []
    assert state.rules[RULE_EXPORT_BELOW_CHEAP].consecutive_hit == 1
    assert state.rules[RULE_EXPORT_BELOW_CHEAP].active is False


def test_discharge_miss_when_battery_idle_and_still_short() -> None:
    firings = _eval(
        _snap(
            exec_mode="export_profit",
            plan_target_net_kwh=5.0,
            remaining_kwh=1.0,
            battery_w=20.0,
            grid_w=100.0,
        )
    )
    hit = next(f for f in firings if f.rule_id == RULE_PLAN_DISCHARGE_MISS)
    assert hit.immediate is False
    assert hit.once_per_hour is True
    assert hit.remind is False


def test_discharge_miss_when_pace_will_not_reach_target() -> None:
    firings = _eval(
        _snap(
            exec_mode="export_profit",
            plan_target_net_kwh=5.0,
            remaining_kwh=1.0,
            battery_w=2000.0,
            time_to_end_s=1800.0,
        ),
        _outlook(pace_remaining_kwh=0.9, pace_dt_s=300.0),
    )
    hit = next(f for f in firings if f.rule_id == RULE_PLAN_DISCHARGE_MISS)
    assert hit.immediate is True
    assert hit.once_per_hour is True
    assert "2.5" in hit.body or "+1.60" in hit.body or "1.60" in hit.body


def test_discharge_miss_ignores_absurd_pace_projection() -> None:
    """Regresja 2026-10-13 19:xx: tempo −48 kWh nie jest sygnałem."""
    firings = _eval(
        _snap(
            exec_mode="export_profit",
            plan_target_net_kwh=3.0,
            remaining_kwh=1.0,
            battery_w=2000.0,
            time_to_end_s=3000.0,
        ),
        # rate ≈ −60 kWh/h → projected ~ −50
        _outlook(pace_remaining_kwh=6.0, pace_dt_s=300.0),
    )
    assert RULE_PLAN_DISCHARGE_MISS not in _ids(firings)


def test_discharge_on_pace_is_silent() -> None:
    # 1.0 kWh now, +0.5 kWh in the last 5 min → 6 kWh/h, 30 min left → +4 kWh, target 2.
    firings = _eval(
        _snap(
            exec_mode="export_profit",
            plan_target_net_kwh=2.0,
            remaining_kwh=1.0,
            battery_w=2000.0,
            time_to_end_s=1800.0,
        ),
        _outlook(pace_remaining_kwh=0.5, pace_dt_s=300.0),
    )
    assert RULE_PLAN_DISCHARGE_MISS not in _ids(firings)


def test_discharge_miss_skipped_soc_hold() -> None:
    firings = _eval(
        _snap(
            exec_mode="export_profit",
            plan_target_net_kwh=5.0,
            remaining_kwh=0.0,
            battery_w=0.0,
            watchdog_reason="export_profit_soc_floor",
        )
    )
    assert RULE_PLAN_DISCHARGE_MISS not in _ids(firings)


def test_planner_silent_fail_no_policy() -> None:
    firings = _eval(_snap(policy_missing=True, exec_mode=None))
    hit = next(f for f in firings if f.rule_id == RULE_PLANNER_SILENT_FAIL)
    assert hit.immediate is True


def test_planner_silent_fail_control_off() -> None:
    firings = _eval(
        _snap(control_enabled=False, exec_mode="export_profit", plan_target_net_kwh=1.0)
    )
    assert RULE_PLANNER_SILENT_FAIL in _ids(firings)


def test_ev_slot_above_cheap_rce_alerts() -> None:
    outlook = _outlook(
        plan_hours=[
            PlanSlot(hour=16, target_net_kwh=0.0, ev_kwh=2.0, rce_pln_kwh=0.80)
        ]
    )
    firings = _eval(_snap(), outlook)
    assert RULE_EV_OUTSIDE_CHEAP in _ids(firings)


def test_ev_slot_covered_at_low_rce_is_silent() -> None:
    outlook = _outlook(
        plan_hours=[
            PlanSlot(hour=11, target_net_kwh=0.0, ev_kwh=3.0, rce_pln_kwh=0.02)
        ]
    )
    firings = _eval(_snap(), outlook)
    assert RULE_EV_OUTSIDE_CHEAP not in _ids(firings)


def test_night_ev_import_is_allowed() -> None:
    outlook = _outlook(
        plan_hours=[
            PlanSlot(hour=23, target_net_kwh=-2.0, ev_kwh=2.0, rce_pln_kwh=0.10)
        ]
    )
    firings = _eval(_snap(), outlook)
    assert RULE_EV_OUTSIDE_CHEAP not in _ids(firings)
    assert RULE_PLAN_IMPORT not in _ids(firings)


def test_planned_import_is_retired_as_noise() -> None:
    """plan_import_unwanted wycofany — rano plan bywa ujemny z EV / charge_grid."""
    outlook = _outlook(
        plan_hours=[
            PlanSlot(hour=11, target_net_kwh=-0.4, ev_kwh=0.0, rce_pln_kwh=0.30),
            PlanSlot(hour=16, target_net_kwh=-0.4, ev_kwh=0.0, rce_pln_kwh=1.20),
        ]
    )
    firings = _eval(_snap(remaining_kwh=0.1, consumption_w=500.0, charging_kw=0.0), outlook)
    assert RULE_PLAN_IMPORT not in _ids(firings)


def test_planned_import_after_13_is_silent() -> None:
    slot = PlanSlot(hour=14, target_net_kwh=-0.52, ev_kwh=0.8, rce_pln_kwh=-0.01)
    firings = _eval(_snap(), _outlook(day_armed=True, plan_hours=[slot]))
    assert RULE_PLAN_IMPORT not in _ids(firings)
    assert RULE_EV_OUTSIDE_CHEAP not in _ids(firings)


def test_debounce_holds_five_minutes_then_digest_reminds() -> None:
    firing = AlertFiring(rule_id="net_negative_expensive", title="Bilans", body="ujemny")
    state = AlertFileState()
    now = datetime(2026, 9, 27, 11, 0, tzinfo=TZ)
    pushed = 0
    for minute in range(4):
        got = apply_firings(
            state,
            [firing],
            now=now + timedelta(minutes=minute),
            debounce_min=5,
            cooldown_min=5,
            clear_min=2,
        )
        pushed += len(got)
    assert pushed == 0
    fifth = apply_firings(
        state,
        [firing],
        now=now + timedelta(minutes=4),
        debounce_min=5,
        cooldown_min=5,
        clear_min=2,
    )
    assert len(fifth) == 1
    assert fifth[0].title == "Bilans"
    held = apply_firings(
        state,
        [firing],
        now=now + timedelta(minutes=6),
        debounce_min=5,
        cooldown_min=5,
        clear_min=2,
    )
    assert held == []
    reminded = apply_firings(
        state,
        [firing],
        now=now + timedelta(minutes=9),
        debounce_min=5,
        cooldown_min=5,
        clear_min=2,
    )
    assert len(reminded) == 1


def test_new_reason_pushes_before_cooldown() -> None:
    first = AlertFiring(rule_id="net_negative_expensive", title="Bilans", body="a", immediate=True)
    second = AlertFiring(rule_id="export_below_cheap", title="Eksport", body="b", immediate=True)
    state = AlertFileState()
    now = datetime(2026, 9, 27, 11, 0, tzinfo=TZ)
    one = apply_firings(
        state, [first], now=now, debounce_min=5, cooldown_min=5, clear_min=2
    )
    assert one[0].title == "Bilans"
    both = apply_firings(
        state,
        [first, second],
        now=now + timedelta(minutes=1),
        debounce_min=5,
        cooldown_min=5,
        clear_min=2,
    )
    assert len(both) == 1
    assert "Bilans" in both[0].body
    assert "Eksport" in both[0].body
    assert "2 alarmy" in both[0].title


def test_legacy_power_rule_drops_without_a_push() -> None:
    state = AlertFileState(
        rules={
            "ev_expensive_fast": RuleRuntime(
                active=True, title="EV 4.4 kW", body="stary", consecutive_hit=10
            )
        }
    )
    fresh = AlertFiring(
        rule_id="plan_import_unwanted", title="Planowany import", body="14", immediate=True
    )
    now = datetime(2026, 9, 27, 13, 0, tzinfo=TZ)
    got = apply_firings(
        state, [fresh], now=now, debounce_min=5, cooldown_min=5, clear_min=2
    )
    assert got[0].title == "Planowany import"
    assert state.rules["ev_expensive_fast"].active is False


def test_full_battery_export_does_not_repeat_same_hour() -> None:
    firing = AlertFiring(
        rule_id="export_below_cheap",
        title="Eksport",
        body="pełna",
        once_per_hour=True,
        remind=False,
    )
    state = AlertFileState()
    now = datetime(2026, 9, 27, 11, 0, tzinfo=TZ)
    first = apply_firings(
        state, [firing], now=now, debounce_min=1, cooldown_min=5, clear_min=2
    )
    assert len(first) == 1
    again = apply_firings(
        state,
        [firing],
        now=now + timedelta(minutes=10),
        debounce_min=1,
        cooldown_min=5,
        clear_min=2,
    )
    assert again == []
    nxt = apply_firings(
        state,
        [firing],
        now=now + timedelta(hours=1),
        debounce_min=1,
        cooldown_min=5,
        clear_min=2,
    )
    assert len(nxt) == 1


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


def test_process_cycle_pushes_negative_balance_not_ev_power(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    path = tmp_path / "alerts.json"
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "alert_store.send_ntfy", lambda title, body: sent.append((title, body)) or True
    )
    monkeypatch.setattr(
        "alert_store.get_settings",
        lambda: SETTINGS.model_copy(
            update={"anomaly_debounce_min": 1, "anomaly_cooldown_min": 5}
        ),
    )
    t0 = datetime(2026, 9, 27, 11, 10, tzinfo=TZ)
    outlook = _outlook()
    kwargs = dict(
        grid_w=0.0,
        pv_w=6000.0,
        consumption_w=5000.0,
        soc_pct=32.0,
        battery_w=-200.0,
        remaining_kwh=-0.10,
        time_to_end_s=3000.0,
        plan_target_net_kwh=0.0,
        exec_mode="neutral",
        planner_execution_enabled=True,
        policy_missing=False,
        control_enabled=True,
        watchdog_reason="x",
        path=path,
        notify=True,
        outlook=outlook,
    )
    process_cycle_alerts(now=t0, E_twc_kwh=100.0, **kwargs)
    assert len(sent) == 1
    assert "Duży pobór" in sent[0][0]
    process_cycle_alerts(now=t0 + timedelta(minutes=1), E_twc_kwh=100.07, **kwargs)
    assert len(sent) == 1
