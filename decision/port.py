"""Port falownika: odczyt wejść bilansu i zapis nastawy."""

from __future__ import annotations

from typing import Protocol

from decision.logic import BalanceInputs
from decision.setpoint import Setpoint


class InverterPort(Protocol):
    async def read_balance_inputs(self) -> BalanceInputs:
        """Odczyt wejść mózgu. Flaga cudzego programu idzie w ``external_schedule_active``."""

    async def apply(self, setpoint: Setpoint) -> None:
        """Zapis nastawy. ``release`` oddaje falownik jego własnemu programowi."""
