"""Nastawa: moc baterii w watach, limit SOC, oddanie sterowania."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Setpoint:
    """Decyzja mózgu. Moc jest w watach; procent slotu liczy adapter."""

    power_w: float
    soc_limit_pct: int
    release: bool
    reason: str
    mode: str
    duration_s: float


def slot_pct_from_watts(power_w: float, watts_per_percent: float) -> int:
    """Odwrotność ``setpoint_from_pct`` dla całkowitego procentu."""
    if watts_per_percent <= 0:
        raise ValueError("watts_per_percent must be positive")
    return int(round(float(power_w) / float(watts_per_percent)))
