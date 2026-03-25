"""Fixtures dla testów guardiana."""
import sys
from pathlib import Path

import pytest

# Projekt nie jest pakietem instalowanym – dodajemy root do ścieżki
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guardian_logic import BalanceInputs


@pytest.fixture
def default_inputs() -> BalanceInputs:
    """Domyślne wejścia – można nadpisać w testach."""
    return BalanceInputs(
        remaining_kwh=0.0,
        time_to_end_s=1800.0,
        pv_w=2000.0,
        consumption_w=1500.0,
        grid_w=500.0,
        soc_pct=50.0,
        p_inverter_w=8200.0,
        p_battery_w=5000.0,
        current_ecoslot_pct=None,
        slot_active=False,
        hysteresis_start=15.0,
        hysteresis_end=2.0,
        balance_threshold_kw=0.6,
        watts_per_percent=70.0,
        other_eco_slot_active=False,
    )
