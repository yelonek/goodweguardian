"""Symulacja magazynu — ograniczenia SOC i mocy."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from planner.config import (
    PLANNER_BATTERY_ETA,
    PLANNER_BATTERY_KWH,
    PLANNER_SOC_MAX_PCT,
    PLANNER_SOC_MIN_PCT,
    max_battery_kwh_per_hour,
)
from planner.models import HourInputs


def eta_one_way_from_rt(eta_rt: float) -> float:
    """Sprawność jednokierunkowa ze sprawności round-trip: ``√η_rt``.

    Parametr planera ``planner_battery_eta`` / ``BatteryParams.eta`` to **η_rt**
    (łóż → wyjmij). Symetryczny model: charge i discharge dostają po ``√η_rt``,
    więc cykl AC→AC odzyskuje dokładnie ``η_rt`` energii.
    """
    if eta_rt <= 0.0:
        raise ValueError(f"eta_rt must be > 0, got {eta_rt}")
    if eta_rt > 1.0:
        raise ValueError(f"eta_rt must be <= 1, got {eta_rt}")
    return math.sqrt(eta_rt)


@dataclass(frozen=True)
class BatteryParams:
    capacity_kwh: float = PLANNER_BATTERY_KWH
    # Round-trip η (AC→AC). W bilansie SOC używaj ``eta_one_way`` / ``eta_one_way_from_rt``.
    eta: float = PLANNER_BATTERY_ETA
    soc_min_pct: float = PLANNER_SOC_MIN_PCT
    soc_max_pct: float = PLANNER_SOC_MAX_PCT
    max_power_kwh_per_h: float = field(default=0.0)

    def __post_init__(self) -> None:
        if self.max_power_kwh_per_h <= 0:
            object.__setattr__(
                self, "max_power_kwh_per_h", max_battery_kwh_per_hour()
            )

    @property
    def eta_one_way(self) -> float:
        """``√η_rt`` — do ``soc += η₁·ch − dis/η₁``."""
        return eta_one_way_from_rt(self.eta)


def max_power_for_hour(hin: HourInputs, params: BatteryParams) -> float:
    """Maks. energia ładowania/rozładowania w slocie [kWh/h-slot]."""
    frac = hin.hour_fraction if hin.hour_fraction > 0 else 1.0
    return params.max_power_kwh_per_h * min(1.0, max(1e-6, frac))


def soc_kwh(soc_pct: float, params: BatteryParams) -> float:
    return (soc_pct / 100.0) * params.capacity_kwh


def soc_pct_from_kwh(energy_kwh: float, params: BatteryParams) -> float:
    if params.capacity_kwh <= 0:
        return 0.0
    return (energy_kwh / params.capacity_kwh) * 100.0


def battery_delta_from_net(
    *,
    pv_kwh: float,
    load_kwh: float,
    net_kwh: float,
) -> float:
    """Δ magazynu [kWh]: PV − load − net (net+ = eksport)."""
    return pv_kwh - load_kwh - net_kwh


def apply_battery_step(
    soc_pct: float,
    battery_delta_kwh: float,
    params: BatteryParams,
) -> float | None:
    """
    Zwraca nowy SOC [%] po kroku, lub None gdy niedopuszczalne (limity / moc).

    ``battery_delta_kwh`` jest po stronie AC (+ ładuj, − oddawaj).
    ``params.eta`` = η_rt; bilans: ``ΔSOC = +E_ch·√η − E_dis/√η``.
    """
    if params.capacity_kwh <= 0:
        return soc_pct

    eta1 = params.eta_one_way
    cur_kwh = soc_kwh(soc_pct, params)
    if battery_delta_kwh > 0:
        stored = battery_delta_kwh * eta1
        if stored > params.max_power_kwh_per_h + 1e-9:
            return None
        new_kwh = cur_kwh + stored
    elif battery_delta_kwh < 0:
        delivered = (-battery_delta_kwh) / eta1
        if delivered > params.max_power_kwh_per_h + 1e-9:
            return None
        new_kwh = cur_kwh - delivered
    else:
        new_kwh = cur_kwh

    new_pct = soc_pct_from_kwh(new_kwh, params)
    if new_pct < params.soc_min_pct - 1e-6 or new_pct > params.soc_max_pct + 1e-6:
        return None
    return max(params.soc_min_pct, min(params.soc_max_pct, new_pct))
