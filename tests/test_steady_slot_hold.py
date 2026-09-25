"""Pomijanie zapisu eco slotu, gdy ten sam procent i próg SOC już stoją do :59."""

from datetime import datetime
from types import SimpleNamespace

from hourly_balance_run import _days_today_only, slot4_write_action, steady_slot_already_set


def _now() -> datetime:
    return datetime(2026, 9, 24, 13, 40)


def _slot(**overrides: object) -> SimpleNamespace:
    now = _now()
    today = _days_today_only(now)[0]
    base: dict[str, object] = dict(
        start_h=13,
        start_m=0,
        end_h=13,
        end_m=59,
        power=-100,
        soc=50,
        on_off=-2,
        day_bits=1 << today,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_skip_when_power_soc_and_end_match() -> None:
    now = _now()
    assert steady_slot_already_set(
        power_pct=-100,
        slot_soc_pct=50,
        slot=_slot(),
        now=now,
    )


def test_export_profit_one_point_writes() -> None:
    now = _now()
    assert not steady_slot_already_set(
        power_pct=81,
        slot_soc_pct=100,
        slot=_slot(power=80, soc=100),
        now=now,
    )


def test_export_profit_identical_percent_skips() -> None:
    now = _now()
    assert steady_slot_already_set(
        power_pct=80,
        slot_soc_pct=100,
        slot=_slot(power=80, soc=100),
        now=now,
    )


def test_charge_grid_one_point_writes() -> None:
    now = _now()
    assert not steady_slot_already_set(
        power_pct=-99,
        slot_soc_pct=50,
        slot=_slot(power=-100),
        now=now,
    )


def test_soc_one_point_writes() -> None:
    now = _now()
    assert not steady_slot_already_set(
        power_pct=-100,
        slot_soc_pct=51,
        slot=_slot(soc=50),
        now=now,
    )


def test_soak_skips_when_power_soc_and_end_match() -> None:
    now = _now()
    assert steady_slot_already_set(
        power_pct=-100,
        slot_soc_pct=50,
        slot=_slot(),
        now=now,
    )


def test_slot4_write_action_hold_when_slot_already_matches() -> None:
    now = _now()
    assert slot4_write_action(
        control_ok=True,
        write_slot=True,
        slot_active=True,
        power_pct=-100,
        slot_soc_pct=50,
        slot=_slot(),
        now=now,
    ) == "hold"


def test_slot4_write_action_none_when_control_off() -> None:
    now = _now()
    assert slot4_write_action(
        control_ok=False,
        write_slot=True,
        slot_active=True,
        power_pct=-100,
        slot_soc_pct=50,
        slot=_slot(),
        now=now,
    ) == "none"


def test_slot4_write_action_sent_to_disable_active_slot() -> None:
    now = _now()
    assert slot4_write_action(
        control_ok=True,
        write_slot=False,
        slot_active=True,
        power_pct=0,
        slot_soc_pct=100,
        slot=_slot(),
        now=now,
    ) == "sent"


def test_short_window_forces_write() -> None:
    now = _now()
    assert not steady_slot_already_set(
        power_pct=-1,
        slot_soc_pct=10,
        slot=_slot(power=-1, soc=10, end_m=45),
        now=now,
    )
