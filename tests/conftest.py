"""Fixtures dla testów guardiana."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Izolacja: testy nie czytają produkcyjnego state/settings.json.
# Ustawiane PRZED importem modułów, które przy imporcie wołają get_settings().
os.environ.setdefault(
    "GUARDIAN_SETTINGS_PATH",
    str(Path(tempfile.gettempdir()) / f"gg_test_settings_{os.getpid()}.json"),
)
os.environ.setdefault("GUARDIAN_DASHBOARD_WARM", "0")
os.environ.setdefault(
    "KPI_DAILY_TOTALS_PATH",
    str(Path(tempfile.gettempdir()) / f"gg_test_kpi_totals_{os.getpid()}.json"),
)

from decision import BalanceInputs


@pytest.fixture(autouse=True)
def _reset_dashboard_speed_caches():
    """Cache prognozy z jednego testu nie może zasłonić monkeypatchy w następnym."""
    import sys

    def _clear() -> None:
        gd = sys.modules.get("guardian_dashboard")
        if gd is not None:
            gd._combined_forecast_cache = None
            gd._combined_refreshing = False
            gd._pv_pyramid_cache = None
            gd._pyramid_refreshing = False
            gd._kpi_disk = None
        pv = sys.modules.get("pv_forecast")
        if pv is not None:
            pv.clear_solcast_cache()
        scan = sys.modules.get("telemetry_scan")
        if scan is not None:
            scan.clear_telemetry_scan_cache()

    _clear()
    yield
    _clear()


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
        external_schedule_active=False,
    )
