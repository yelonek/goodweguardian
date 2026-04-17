"""Taryfa dwustrefowa G12 — strefa od godziny + składowe PLN/kWh (dystrybucja, opcjonalnie stała energia)."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Zone = Literal["day", "night"]

# G12 Enea — strefa nocna (tańsza): 22:00–6:00 (8 h) oraz 13:00–15:00 (2 h), lokalnie PL,
# wg publikowanych „możliwych grup taryfowych i stref godzinowych” (zegary wg czasu zimowego).
ENEA_G12_NIGHT_HOURS: frozenset[int] = frozenset(
    {0, 1, 2, 3, 4, 5, 13, 14, 22, 23}
)


class G12TariffConfig(BaseModel):
    """
    Noc = tańsza strefa G12 (domyślnie godziny Enea G12 — stałe w module).
    Dzień = pozostałe godziny 0..23.
    """

    night_hours: frozenset[int] = Field(
        default=ENEA_G12_NIGHT_HOURS,
        description="Godziny strefy nocnej (tańszej), lokalnie 0..23",
    )
    distribution_day_pln_per_kwh: float = Field(
        0.0,
        ge=0.0,
        description="Składowa dystrybucji (i ewent. stałe zmienne OSD) w strefie dziennej za kWh",
    )
    distribution_night_pln_per_kwh: float = Field(
        0.0,
        ge=0.0,
        description="Jak wyżej — strefa nocna",
    )
    energy_from_rce: bool = Field(
        True,
        description="True: do ceny doliczyć RCE (PLN/kWh) z godziny; False: użyć stałych energii poniżej",
    )
    energy_day_pln_per_kwh: float = Field(
        0.0,
        ge=0.0,
        description="Stała cena energii za kWh w strefie dziennej (gdy energy_from_rce=False)",
    )
    energy_night_pln_per_kwh: float = Field(
        0.0,
        ge=0.0,
        description="Stała cena energii za kWh w strefie nocnej (gdy energy_from_rce=False)",
    )

    @model_validator(mode="after")
    def _night_not_all(self) -> G12TariffConfig:
        if len(self.night_hours) >= 24:
            raise ValueError("night_hours nie może obejmować wszystkich 24 godzin")
        return self

    def zone_for_hour(self, hour: int) -> Zone:
        if not 0 <= hour <= 23:
            raise ValueError(f"hour musi być 0..23, jest {hour}")
        return "night" if hour in self.night_hours else "day"

    def distribution_pln_per_kwh(self, zone: Zone) -> float:
        return (
            self.distribution_night_pln_per_kwh
            if zone == "night"
            else self.distribution_day_pln_per_kwh
        )

    def energy_pln_per_kwh(self, zone: Zone, rce_pln_per_kwh: float) -> float:
        if self.energy_from_rce:
            return rce_pln_per_kwh
        return (
            self.energy_night_pln_per_kwh
            if zone == "night"
            else self.energy_day_pln_per_kwh
        )

    def effective_import_pln_per_kwh(
        self, local_hour: int, rce_pln_per_kwh: float
    ) -> float:
        z = self.zone_for_hour(local_hour)
        return self.distribution_pln_per_kwh(z) + self.energy_pln_per_kwh(z, rce_pln_per_kwh)


def g12_tariff_from_env() -> G12TariffConfig:
    """
    Zmienne środowiskowe:
      TARIFF_DISTRIBUTION_DAY_PLN_KWH
      TARIFF_DISTRIBUTION_NIGHT_PLN_KWH
      TARIFF_ENERGY_FROM_RCE — true/false (domyślnie true)
      TARIFF_ENERGY_DAY_PLN_KWH / TARIFF_ENERGY_NIGHT_PLN_KWH — gdy energia nie z RCE

    Godziny strefy nocnej G12 są stałe: ``ENEA_G12_NIGHT_HOURS`` (Enea).
    """

    def _f(name: str, default: float) -> float:
        v = os.environ.get(name)
        if v is None or v.strip() == "":
            return default
        return float(v.replace(",", "."))

    def _b(name: str, default: bool) -> bool:
        v = os.environ.get(name)
        if v is None or v.strip() == "":
            return default
        return v.strip().lower() in ("1", "true", "yes", "on", "y")

    return G12TariffConfig(
        distribution_day_pln_per_kwh=_f("TARIFF_DISTRIBUTION_DAY_PLN_KWH", 0.0),
        distribution_night_pln_per_kwh=_f("TARIFF_DISTRIBUTION_NIGHT_PLN_KWH", 0.0),
        energy_from_rce=_b("TARIFF_ENERGY_FROM_RCE", True),
        energy_day_pln_per_kwh=_f("TARIFF_ENERGY_DAY_PLN_KWH", 0.0),
        energy_night_pln_per_kwh=_f("TARIFF_ENERGY_NIGHT_PLN_KWH", 0.0),
    )
