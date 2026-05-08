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
    energy_day_pln_per_kwh: float = Field(
        0.0,
        ge=0.0,
        description="Stała cena energii (sprzedawca) za kWh w strefie dziennej — płacona przy imporcie netto w tej godzinie",
    )
    energy_night_pln_per_kwh: float = Field(
        0.0,
        ge=0.0,
        description="Jak wyżej — strefa nocna",
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

    def fixed_energy_pln_per_kwh(self, zone: Zone) -> float:
        """Energia ze stałej taryfy G12 (dzień/noc), bez RCE — strona importu."""
        return (
            self.energy_night_pln_per_kwh
            if zone == "night"
            else self.energy_day_pln_per_kwh
        )

    def import_pln_per_kwh(self, local_hour: int) -> float:
        """
        Szacunkowy koszt 1 kWh importu netto w tej godzinie: dystrybucja (strefa) + energia (strefa).
        Przy eksporcie netto stosuje się RCE (osobno), nie ta wartość.
        """
        z = self.zone_for_hour(local_hour)
        return self.distribution_pln_per_kwh(z) + self.fixed_energy_pln_per_kwh(z)

    def effective_import_pln_per_kwh(
        self, local_hour: int, rce_pln_per_kwh: float
    ) -> float:
        """Zgodność wsteczna: ignoruje RCE; to samo co ``import_pln_per_kwh``."""
        _ = rce_pln_per_kwh
        return self.import_pln_per_kwh(local_hour)


def g12_tariff_from_env() -> G12TariffConfig:
    """
    Zmienne środowiskowe:
      TARIFF_DISTRIBUTION_DAY_PLN_KWH / TARIFF_DISTRIBUTION_NIGHT_PLN_KWH
      TARIFF_ENERGY_DAY_PLN_KWH / TARIFF_ENERGY_NIGHT_PLN_KWH

    Strefa dzień/noc wg godzin G12: ``ENEA_G12_NIGHT_HOURS`` (Enea: 22–6 i 13–15 zimą).
    Import netto w godzinie: dystrybucja + energia wg strefy. Eksport netto: rozliczenie RCE (poza tym modelem).
    """

    def _f(name: str, default: float) -> float:
        v = os.environ.get(name)
        if v is None or v.strip() == "":
            return default
        return float(v.replace(",", "."))

    return G12TariffConfig(
        distribution_day_pln_per_kwh=_f("TARIFF_DISTRIBUTION_DAY_PLN_KWH", 0.0),
        distribution_night_pln_per_kwh=_f("TARIFF_DISTRIBUTION_NIGHT_PLN_KWH", 0.0),
        energy_day_pln_per_kwh=_f("TARIFF_ENERGY_DAY_PLN_KWH", 0.0),
        energy_night_pln_per_kwh=_f("TARIFF_ENERGY_NIGHT_PLN_KWH", 0.0),
    )
