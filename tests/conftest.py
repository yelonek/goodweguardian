"""Fixtures dla testów guardiana."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guardian_logic import BalanceInputs


@pytest.fixture(autouse=True)
def _planner_scenario_off_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Testy planera domyślnie deterministyczne p50 (niezależnie od .env produkcyjnego)."""
    import planner.config as cfg

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "off")


@pytest.fixture
def default_inputs() -> BalanceInputs:
    """Domyślne wejścia – można nadpisać w testach."""
    return BalanceInputs(
        remaining_kwh=0.0,
        time_to_end_s=1800.0,
        pv_w=2000.0,
        consumption_w=1500.0,
        soc_pct=50.0,
        p_inverter_w=8200.0,
        p_battery_w=5000.0,
        watts_per_percent=70.0,
        other_eco_slot_active=False,
    )
