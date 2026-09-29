"""Detektory anomalii: bilans godziny, tani eksport, egzekucja planu, zapowiedź.

Czyste funkcje: ``evaluate(snapshot, outlook)`` → lista firingów.
Debounce, zbiorczy push i odczyt planu są w ``alert_store``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from guardian_settings import GuardianSettings, get_settings
from tariff_g12 import G12TariffConfig, g12_tariff_from_env

RULE_NET_NEGATIVE_EXPENSIVE = "net_negative_expensive"
RULE_NET_NEGATIVE_MIDDAY = "net_negative_midday"
RULE_EXPORT_BELOW_CHEAP = "export_below_cheap"
RULE_PLAN_DISCHARGE_MISS = "plan_discharge_miss"
RULE_PLANNER_SILENT_FAIL = "planner_silent_fail"
RULE_EV_OUTSIDE_CHEAP = "ev_outside_cheap"
RULE_PLAN_IMPORT = "plan_import_unwanted"

# Żywy bilans godziny. Pasmo pięciu minut nie przechodzi przez :00.
LIVE_HOUR_RULES = frozenset(
    {
        RULE_NET_NEGATIVE_EXPENSIVE,
        RULE_NET_NEGATIVE_MIDDAY,
        RULE_EXPORT_BELOW_CHEAP,
        RULE_PLAN_DISCHARGE_MISS,
    }
)

CURRENT_RULES = frozenset(
    {
        RULE_NET_NEGATIVE_EXPENSIVE,
        RULE_NET_NEGATIVE_MIDDAY,
        RULE_EXPORT_BELOW_CHEAP,
        RULE_PLAN_DISCHARGE_MISS,
        RULE_PLANNER_SILENT_FAIL,
        RULE_EV_OUTSIDE_CHEAP,
        RULE_PLAN_IMPORT,
    }
)

# Do 13:00 ujemny bilans alarmuje sam. Od 13:00 do 22:00 tylko duży pobór.
WATCH_HOURS = frozenset(range(6, 13))
ABNORMAL_LOAD_HOURS = frozenset(range(13, 22))
ABNORMAL_HOUSE_W = 2500.0
ABNORMAL_EV_KW = 1.0

# Ładowanie 6:00–15:00 (godziny 6–14) zostaje w outlooku jako day_armed.
ARM_HOURS = frozenset(range(6, 15))
MIDDAY_HOURS = frozenset({13, 14})

# Poniżej tego bateria nie „jedzie” w eksporcie zarobkowym.
BATTERY_DISCHARGE_W = 150.0

# Okno tempa: próbka sprzed około pięciu minut.
PACE_MIN_S = 4.5 * 60.0
PACE_MAX_S = 6.5 * 60.0

MAX_TWC_SAMPLE_GAP_S = 180.0

SOC_HOLD_REASONS = frozenset(
    {
        "soc_full_defense_hold",
        "soc_full_defense_carryover",
        "night_soc_reserve_hold",
        "export_profit_soc_floor",
    }
)


class AnomalySnapshot(BaseModel):
    local_hour: int = Field(ge=0, le=23)
    local_minute: int = Field(ge=0, le=59)
    grid_w: float
    pv_w: float
    consumption_w: float
    soc_pct: float
    battery_w: float
    remaining_kwh: float
    time_to_end_s: float
    plan_target_net_kwh: float | None = None
    exec_mode: str | None = None
    planner_execution_enabled: bool = False
    policy_missing: bool = False
    control_enabled: bool = True
    watchdog_reason: str = ""
    charging_kw: float | None = None


class PlanSlot(BaseModel):
    """Godzina z rolling planu: bilans, EV i RCE. Tylko bieżąca i przyszłe."""

    hour: int = Field(ge=0, le=23)
    target_net_kwh: float | None = None
    ev_kwh: float = 0.0
    rce_pln_kwh: float | None = None


class AlertOutlook(BaseModel):
    """Fakty z planu, RCE i historii tempa. Detektor ich nie czyta z dysku."""

    rce_pln_kwh: float | None = None
    cheap_import_pln_kwh: float = 0.59
    soc_full_pct: float = 99.0
    export_buffer_kwh: float = 0.2
    day_armed: bool = False
    pace_remaining_kwh: float | None = None
    pace_dt_s: float | None = None
    plan_hours: list[PlanSlot] = Field(default_factory=list)


class AlertFiring(BaseModel):
    rule_id: str
    title: str
    body: str
    est_pln_per_h: float = 0.0
    # Plan i ślepe sterowanie wchodzą od razu. Tempo / bilans czekają debounce.
    immediate: bool = False
    remind: bool = True
    # Pełna bateria: jeden push na godzinę zegarową, bez powtórki co 5 minut.
    once_per_hour: bool = False


def charging_kw_from_twc(
    prev_kwh: float | None,
    now_kwh: float | None,
    dt_s: float,
) -> float | None:
    """Moc EV z przyrostu licznika TWC. ``None`` gdy brak próbek albo dziura > 3 min."""
    if prev_kwh is None or now_kwh is None:
        return None
    if dt_s <= 0 or dt_s > MAX_TWC_SAMPLE_GAP_S:
        return None
    delta = max(0.0, float(now_kwh) - float(prev_kwh))
    return delta * (3600.0 / dt_s)


def day_is_armed(
    actual_kwh_by_hour: dict[int, float],
    planned_kwh_by_hour: dict[int, float],
) -> bool:
    """Dzień z ładowaniem 6:00–15:00: faktyczne kWh albo slot planu w godzinach 6–14."""
    for hour in ARM_HOURS:
        if float(actual_kwh_by_hour.get(hour, 0.0)) > 1e-3:
            return True
        if float(planned_kwh_by_hour.get(hour, 0.0)) > 1e-3:
            return True
    return False


def _reason_is_soc_hold(reason: str) -> bool:
    base = (reason or "").split("+", 1)[0].strip()
    return base in SOC_HOLD_REASONS


def _net_negative(remaining_kwh: float) -> bool:
    return float(remaining_kwh) < -1e-6


def _projected_net(snap: AnomalySnapshot, outlook: AlertOutlook) -> float | None:
    dt_s = outlook.pace_dt_s
    then = outlook.pace_remaining_kwh
    if dt_s is None or then is None:
        return None
    if dt_s < PACE_MIN_S or dt_s > PACE_MAX_S:
        return None
    rate = (float(snap.remaining_kwh) - float(then)) / (dt_s / 3600.0)
    return float(snap.remaining_kwh) + rate * (float(snap.time_to_end_s) / 3600.0)


def _below_target(value: float | None, target: float | None) -> bool:
    if value is None or target is None:
        return False
    return float(value) + 1e-3 < float(target)


def _firing(
    rule_id: str,
    title: str,
    body: str,
    *,
    immediate: bool = False,
    remind: bool = True,
    once_per_hour: bool = False,
) -> AlertFiring:
    return AlertFiring(
        rule_id=rule_id,
        title=title,
        body=body,
        immediate=immediate,
        remind=remind,
        once_per_hour=once_per_hour,
    )


def _house_load_w(snap: AnomalySnapshot) -> float:
    """Pobór domu. Auto z TWC schodzi z consumption, gdy próbka jest świeża."""
    ev_w = 0.0
    if snap.charging_kw is not None:
        ev_w = max(0.0, float(snap.charging_kw)) * 1000.0
    return float(snap.consumption_w) - ev_w


def _abnormal_draw(snap: AnomalySnapshot, *, night: frozenset[int]) -> list[str]:
    """Grzałka albo auto poza tanim G12. Zwykły dom (~0,5 kW) tu nie wchodzi."""
    reasons: list[str] = []
    house_w = _house_load_w(snap)
    if house_w > ABNORMAL_HOUSE_W:
        reasons.append(f"pobór domu {house_w / 1000.0:.1f} kW")
    ev = snap.charging_kw
    if (
        ev is not None
        and float(ev) >= ABNORMAL_EV_KW
        and snap.local_hour not in night
    ):
        reasons.append(f"auto {float(ev):.1f} kW")
    return reasons


def _net_negative_live(
    snap: AnomalySnapshot,
    *,
    zone: str,
    night: frozenset[int],
) -> AlertFiring | None:
    if not _net_negative(snap.remaining_kwh):
        return None
    hour = snap.local_hour
    bal = f"Bilans godziny {hour:02d} jest {snap.remaining_kwh:+.2f} kWh."
    if hour in WATCH_HOURS:
        if zone != "day":
            return None
        return _firing(
            RULE_NET_NEGATIVE_EXPENSIVE,
            f"Bilans godziny {hour:02d} poniżej zera",
            bal,
        )
    if hour not in ABNORMAL_LOAD_HOURS:
        return None
    reasons = _abnormal_draw(snap, night=night)
    if not reasons:
        return None
    rule = (
        RULE_NET_NEGATIVE_MIDDAY if hour in MIDDAY_HOURS else RULE_NET_NEGATIVE_EXPENSIVE
    )
    return _firing(
        rule,
        f"Duży pobór o {hour:02d}",
        bal + " " + "; ".join(reasons) + ".",
    )


def _export_below_cheap(
    snap: AnomalySnapshot, outlook: AlertOutlook
) -> AlertFiring | None:
    """kWh ponad bufor soak, nie chwilowe waty na starcie godziny."""
    if (snap.exec_mode or "") == "export_profit":
        return None
    buffer = float(outlook.export_buffer_kwh)
    if float(snap.remaining_kwh) <= buffer + 1e-9:
        return None
    rce = outlook.rce_pln_kwh
    cheap = float(outlook.cheap_import_pln_kwh)
    if rce is None or float(rce) + 1e-9 >= cheap:
        return None
    full = float(snap.soc_pct) + 1e-9 >= float(outlook.soc_full_pct)
    body = (
        f"Bilans godziny {snap.local_hour:02d} jest {snap.remaining_kwh:+.2f} kWh, "
        f"bufor {buffer:.2f} kWh. RCE {float(rce):.2f} zł, tani import {cheap:.2f} zł."
    )
    if full:
        body += " Bateria pełna — PV nie ma gdzie zostać."
    else:
        body += f" SOC {snap.soc_pct:.0f}% — jest gdzie to schować."
    return _firing(
        RULE_EXPORT_BELOW_CHEAP,
        "Eksport poniżej taniego importu",
        body,
        remind=not full,
        once_per_hour=full,
    )


def _plan_discharge_miss(
    snap: AnomalySnapshot, outlook: AlertOutlook
) -> AlertFiring | None:
    if (snap.exec_mode or "") != "export_profit":
        return None
    if _reason_is_soc_hold(snap.watchdog_reason):
        return None
    target = snap.plan_target_net_kwh
    if target is None:
        return None
    still_short = _below_target(snap.remaining_kwh, target)
    if not still_short:
        return None
    battery_idle = float(snap.battery_w) < BATTERY_DISCHARGE_W
    projected = _projected_net(snap, outlook)
    pace_miss = _below_target(projected, target)
    if not battery_idle and not pace_miss:
        return None
    if pace_miss and projected is not None:
        detail = (
            f"Tempo daje około {projected:+.2f} kWh na koniec, plan {float(target):+.2f} kWh."
        )
    else:
        detail = (
            f"Bateria {snap.battery_w:.0f} W, bilans {snap.remaining_kwh:+.2f} kWh, "
            f"plan {float(target):+.2f} kWh."
        )
    return _firing(
        RULE_PLAN_DISCHARGE_MISS,
        "Bateria nie oddaje według planu",
        detail,
        immediate=pace_miss,
    )


def _planner_silent_fail(snap: AnomalySnapshot) -> AlertFiring | None:
    if not snap.planner_execution_enabled:
        return None
    if snap.policy_missing:
        return _firing(
            RULE_PLANNER_SILENT_FAIL,
            "Planer bez policy",
            "PLANNER_EXECUTION włączony, ale brak ważnego wiersza policy. "
            "Guardian spadł na bilans ~0.",
            immediate=True,
        )
    mode = snap.exec_mode
    if (
        not snap.control_enabled
        and mode not in (None, "neutral")
        and not _reason_is_soc_hold(snap.watchdog_reason)
    ):
        return _firing(
            RULE_PLANNER_SILENT_FAIL,
            "Sterowanie wyłączone przy aktywnym planie",
            f"Sterowanie wyłączone, a plan chce mode={mode}.",
            immediate=True,
        )
    return None


def _slot_ev_is_cheap(slot: PlanSlot, *, night: frozenset[int], cheap: float) -> bool:
    if float(slot.ev_kwh) <= 1e-3:
        return True
    if slot.hour in night and slot.hour not in MIDDAY_HOURS:
        return True
    if slot.hour in MIDDAY_HOURS:
        return True
    rce = slot.rce_pln_kwh
    if rce is None or slot.target_net_kwh is None:
        return False
    covers = float(slot.target_net_kwh) >= -1e-6 and float(rce) < cheap
    return covers


def _ev_outside_cheap(
    outlook: AlertOutlook, *, night: frozenset[int]
) -> AlertFiring | None:
    cheap = float(outlook.cheap_import_pln_kwh)
    bad: list[str] = []
    for slot in outlook.plan_hours:
        if float(slot.ev_kwh) <= 1e-3:
            continue
        if _slot_ev_is_cheap(slot, night=night, cheap=cheap):
            continue
        rce = "brak RCE" if slot.rce_pln_kwh is None else f"RCE {slot.rce_pln_kwh:.2f} zł"
        net = "brak planu" if slot.target_net_kwh is None else f"net {slot.target_net_kwh:+.2f} kWh"
        bad.append(f"{slot.hour:02d}: {slot.ev_kwh:.1f} kWh, {rce}, {net}")
    if not bad:
        return None
    return _firing(
        RULE_EV_OUTSIDE_CHEAP,
        "EV poza tanią energią",
        "Sloty EV, które nie są nocą 22–6 ani nadwyżką przy RCE poniżej taniego importu: "
        + "; ".join(bad)
        + ".",
        immediate=True,
    )


def _plan_import(outlook: AlertOutlook) -> AlertFiring | None:
    """Tylko 6:00–13:00. Wieczór z wysokim RCE i zwykłym loadem nie jest alarmem."""
    lines: list[str] = []
    for slot in outlook.plan_hours:
        target = slot.target_net_kwh
        if target is None or float(target) >= -1e-6:
            continue
        if slot.hour not in WATCH_HOURS:
            continue
        lines.append(
            f"{slot.hour:02d}: net {float(target):+.2f} kWh (droga strefa G12)"
        )
    if not lines:
        return None
    return _firing(
        RULE_PLAN_IMPORT,
        "Planowany import",
        "Plan schodzi poniżej zera: " + "; ".join(lines) + ".",
        immediate=True,
    )


def evaluate(
    snap: AnomalySnapshot,
    outlook: AlertOutlook | None = None,
    *,
    settings: GuardianSettings | None = None,
    tariff: G12TariffConfig | None = None,
) -> list[AlertFiring]:
    s = settings if settings is not None else get_settings()
    if not s.anomaly_alerts_enabled:
        return []
    t = tariff if tariff is not None else g12_tariff_from_env()
    view = outlook if outlook is not None else AlertOutlook()
    zone = t.zone_for_hour(snap.local_hour)
    night = frozenset(t.night_hours)

    out: list[AlertFiring] = []
    for firing in (
        _net_negative_live(snap, zone=zone, night=night),
        _export_below_cheap(snap, view),
        _plan_discharge_miss(snap, view),
        _planner_silent_fail(snap),
        _ev_outside_cheap(view, night=night),
        _plan_import(view),
    ):
        if firing is not None:
            out.append(firing)
    return out
