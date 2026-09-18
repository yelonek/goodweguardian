"""Detektory anomalii, które palą kasę albo tracą zysk z planu.

Czyste funkcje: ``evaluate(snapshot)`` → lista firingów. Debounce / ntfy / persist
są w ``alert_store``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from guardian_settings import GuardianSettings, get_settings
from tariff_g12 import G12TariffConfig, g12_tariff_from_env

RULE_EV_EXPENSIVE_FAST = "ev_expensive_fast"
RULE_GRID_IMPORT_PV = "grid_import_pv_expensive"
RULE_PLAN_DISCHARGE_MISS = "plan_discharge_miss"
RULE_PLANNER_SILENT_FAIL = "planner_silent_fail"

INTENTIONAL_IMPORT_MODES = frozenset({"charge_grid", "import_grid"})
EXPORT_MODES = frozenset({"export_profit", "export_pv_surplus"})
SOC_HOLD_REASONS = frozenset(
    {
        "soc_full_defense_hold",
        "soc_full_defense_carryover",
        "night_soc_reserve_hold",
        "export_profit_soc_floor",
    }
)

# Szum mocy baterii — poniżej tego nie uważamy, że rozładowuje.
BATTERY_IDLE_W = 150.0
MAX_TWC_SAMPLE_GAP_S = 180.0


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


class AlertFiring(BaseModel):
    rule_id: str
    title: str
    body: str
    est_pln_per_h: float = 0.0


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


def _import_kw(grid_w: float) -> float:
    return max(0.0, -float(grid_w) / 1000.0)


def _est_pln_per_h(grid_w: float, import_pln: float) -> float:
    return _import_kw(grid_w) * float(import_pln)


def _cash_gate_ok(est_pln_per_h: float, threshold: float, import_pln: float) -> bool:
    """Gdy taryfa w settings = 0, nie blokuj detektorów mocy."""
    if float(import_pln) <= 1e-9:
        return True
    return float(est_pln_per_h) + 1e-9 >= float(threshold)


def _reason_is_soc_hold(reason: str) -> bool:
    base = (reason or "").split("+", 1)[0].strip()
    return base in SOC_HOLD_REASONS


def _plan_lag_kwh(snap: AnomalySnapshot) -> float | None:
    """Dodatnie = bilans zostaje za liniowym tempem do ``target_net``."""
    target = snap.plan_target_net_kwh
    if target is None:
        return None
    elapsed_s = max(0.0, 3600.0 - float(snap.time_to_end_s))
    frac = min(1.0, elapsed_s / 3600.0)
    expected = float(target) * frac
    return expected - float(snap.remaining_kwh)


def _ev_expensive_fast(
    snap: AnomalySnapshot, s: GuardianSettings, *, zone: str, import_pln: float
) -> AlertFiring | None:
    if zone != "day":
        return None
    kw = snap.charging_kw
    if kw is None or kw + 1e-9 < float(s.anomaly_ev_kw_threshold):
        return None
    est = _est_pln_per_h(snap.grid_w, import_pln)
    if est < 1e-9 and import_pln > 1e-9:
        est = float(kw) * float(import_pln)
    title = f"EV {kw:.1f} kW w drogiej taryfie"
    body = (
        f"Ładowanie {kw:.1f} kW o {snap.local_hour:02d}:{snap.local_minute:02d} "
        f"(G12 dzień). Szacunek ~{est:.1f} zł/h — zwolnij w Tesli."
    )
    return AlertFiring(
        rule_id=RULE_EV_EXPENSIVE_FAST, title=title, body=body, est_pln_per_h=est
    )


def _grid_import_pv(
    snap: AnomalySnapshot, s: GuardianSettings, *, zone: str, import_pln: float
) -> AlertFiring | None:
    if zone != "day":
        return None
    if (snap.exec_mode or "") in INTENTIONAL_IMPORT_MODES:
        return None
    if float(snap.pv_w) + 1e-9 < float(s.anomaly_pv_w_threshold):
        return None
    if float(snap.grid_w) > -float(s.anomaly_import_w_threshold):
        return None
    est = _est_pln_per_h(snap.grid_w, import_pln)
    if not _cash_gate_ok(est, float(s.anomaly_cash_pln_per_h), import_pln):
        return None
    causes: list[str] = []
    if snap.charging_kw is not None and snap.charging_kw >= 1.0:
        causes.append(f"EV {snap.charging_kw:.1f} kW")
    if snap.battery_w < -BATTERY_IDLE_W:
        causes.append("bateria ładuje")
    elif snap.battery_w < BATTERY_IDLE_W:
        causes.append("bateria stoi")
    cause_txt = ", ".join(causes) if causes else "load domu"
    import_kw = _import_kw(snap.grid_w)
    title = "Import z sieci przy PV w drogiej taryfie"
    body = (
        f"Sieć {import_kw:.1f} kW import, PV {snap.pv_w / 1000.0:.1f} kW, "
        f"{cause_txt}. ~{est:.1f} zł/h o {snap.local_hour:02d}:{snap.local_minute:02d}."
    )
    return AlertFiring(
        rule_id=RULE_GRID_IMPORT_PV, title=title, body=body, est_pln_per_h=est
    )


def _plan_discharge_miss(
    snap: AnomalySnapshot, s: GuardianSettings, *, import_pln: float
) -> AlertFiring | None:
    if (snap.exec_mode or "") not in EXPORT_MODES:
        return None
    if snap.local_minute < int(s.anomaly_plan_miss_after_minute):
        return None
    if _reason_is_soc_hold(snap.watchdog_reason):
        return None
    if float(snap.battery_w) >= BATTERY_IDLE_W:
        return None
    lag = _plan_lag_kwh(snap)
    if lag is None or lag + 1e-9 < float(s.anomaly_plan_miss_kwh):
        return None
    target = float(snap.plan_target_net_kwh or 0.0)
    est = _est_pln_per_h(snap.grid_w, import_pln)
    title = "Bateria nie rozładowuje według planu"
    body = (
        f"mode={snap.exec_mode} target {target:+.2f} kWh, bilans {snap.remaining_kwh:+.2f} "
        f"(zaległość {lag:.2f} kWh), bateria {snap.battery_w:.0f} W, SOC {snap.soc_pct:.0f}%."
    )
    return AlertFiring(
        rule_id=RULE_PLAN_DISCHARGE_MISS, title=title, body=body, est_pln_per_h=est
    )


def _planner_silent_fail(snap: AnomalySnapshot) -> AlertFiring | None:
    if not snap.planner_execution_enabled:
        return None
    if snap.policy_missing:
        title = "Planer bez policy — cichy Flappy"
        body = (
            "PLANNER_EXECUTION włączony, ale brak ważnego wiersza policy "
            "(degraded / wygasły / dziura). Guardian spadł na bilans ~0."
        )
        return AlertFiring(
            rule_id=RULE_PLANNER_SILENT_FAIL, title=title, body=body
        )
    mode = snap.exec_mode
    if (
        not snap.control_enabled
        and mode not in (None, "neutral")
        and not _reason_is_soc_hold(snap.watchdog_reason)
    ):
        title = "Sterowanie wyłączone przy aktywnym planie"
        body = (
            f"control_off, a plan chce mode={mode}. Inwerter nie dostaje eco slotu."
        )
        return AlertFiring(
            rule_id=RULE_PLANNER_SILENT_FAIL, title=title, body=body
        )
    return None


def evaluate(
    snap: AnomalySnapshot,
    *,
    settings: GuardianSettings | None = None,
    tariff: G12TariffConfig | None = None,
) -> list[AlertFiring]:
    s = settings if settings is not None else get_settings()
    if not s.anomaly_alerts_enabled:
        return []
    t = tariff if tariff is not None else g12_tariff_from_env()
    zone = t.zone_for_hour(snap.local_hour)
    import_pln = t.import_pln_per_kwh(snap.local_hour)

    out: list[AlertFiring] = []
    for fn in (
        _ev_expensive_fast(snap, s, zone=zone, import_pln=import_pln),
        _grid_import_pv(snap, s, zone=zone, import_pln=import_pln),
        _plan_discharge_miss(snap, s, import_pln=import_pln),
        _planner_silent_fail(snap),
    ):
        if fn is not None:
            out.append(fn)
    return out
