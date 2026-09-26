"""Adapter Deye. Zapis rejestrów jest zamknięty, dopóki nie ma modelu i mapy."""

from __future__ import annotations

import sys

from decision.logic import BalanceInputs
from decision.setpoint import Setpoint

MAP_MISSING = "DeyeDefender: brak mapy rejestrów, pętla nie startuje."


class DeyeMapMissing(RuntimeError):
    """Nie ma modelu ani mapy rejestrów. Odczyt i zapis nie idą na falownik."""


class DeyeDefender:
    """Ten sam port co Guardian. Odczyt i zapis odmawiają, zanim powstanie pętla."""

    async def read_balance_inputs(self) -> BalanceInputs:
        raise DeyeMapMissing(MAP_MISSING)

    async def apply(self, setpoint: Setpoint) -> None:
        del setpoint
        raise DeyeMapMissing(MAP_MISSING)


def main() -> None:
    """Koniec zanim wstanie pętla. Nie łączy się z falownikiem."""
    sys.stderr.write(MAP_MISSING + "\n")
    raise SystemExit(1)
