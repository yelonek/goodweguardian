"""Symulacja magazynu — ograniczenia SOC i mocy."""

from __future__ import annotations

from dataclasses import dataclass, field

from planner.config import (
    PLANNER_BATTERY_ETA,
    PLANNER_BATTERY_KWH,
    PLANNER_SOC_MAX_PCT,
    PLANNER_SOC_MIN_PCT,
    max_battery_kwh_per_hour,
)


@dataclass(frozen=True)
class BatteryParams:
    capacity_kwh: float = PLANNER_BATTERY_KWH
    eta: float = PLANNER_BATTERY_ETA
    soc_min_pct: float = PLANNER_SOC_MIN_PCT
    soc_max_pct: float = PLANNER_SOC_MAX_PCT
    max_power_kwh_per_h: float = field(default=0.0)

    def __post_init__(self) -> None:
        if self.max_power_kwh_per_h <= 0:
            object.__setattr__(
                self, "max_power_kwh_per_h", max_battery_kwh_per_hour()
            )


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
    Ładowanie zużywa eta; rozładowanie dostarcza z eta.
    """
    if params.capacity_kwh <= 0:
        return soc_pct

    cur_kwh = soc_kwh(soc_pct, params)
    if battery_delta_kwh > 0:
        stored = battery_delta_kwh * params.eta
        if stored > params.max_power_kwh_per_h + 1e-9:
            return None
        new_kwh = cur_kwh + stored
    elif battery_delta_kwh < 0:
        delivered = (-battery_delta_kwh) / params.eta
        if delivered > params.max_power_kwh_per_h + 1e-9:
            return None
        new_kwh = cur_kwh - delivered
    else:
        new_kwh = cur_kwh

    new_pct = soc_pct_from_kwh(new_kwh, params)
    if new_pct < params.soc_min_pct - 1e-6 or new_pct > params.soc_max_pct + 1e-6:
        return None
    return max(params.soc_min_pct, min(params.soc_max_pct, new_pct))
