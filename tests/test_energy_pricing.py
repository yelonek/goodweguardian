"""Integracja energy_pricing z taryfą G12 (import bez RCE w cenie energii)."""

from datetime import date
from pathlib import Path

import pytest

from energy_pricing import effective_import_pln_per_kwh, hourly_effective_import_pln_per_kwh_for_day
from tariff_g12 import G12TariffConfig


@pytest.fixture
def sample_tariff() -> G12TariffConfig:
    # Domyślne godziny nocne = ENEA G12 (w module); 15 = dzień, 22 = noc
    return G12TariffConfig(
        distribution_day_pln_per_kwh=0.35,
        distribution_night_pln_per_kwh=0.18,
        energy_day_pln_per_kwh=1.0,
        energy_night_pln_per_kwh=0.5,
    )


def test_import_tariff_per_hour(sample_tariff: G12TariffConfig, tmp_path: Path) -> None:
    d = date(2026, 4, 1)
    v_day = effective_import_pln_per_kwh(
        d, 15, tariff=sample_tariff, client=None, force_refresh_rce=False
    )
    v_night = effective_import_pln_per_kwh(
        d, 22, tariff=sample_tariff, client=None, force_refresh_rce=False
    )

    assert v_day == pytest.approx(0.35 + 1.0)
    assert v_night == pytest.approx(0.18 + 0.5)


def test_hourly_row_length(sample_tariff: G12TariffConfig) -> None:
    row = hourly_effective_import_pln_per_kwh_for_day(
        date(2026, 4, 1), tariff=sample_tariff
    )
    assert len(row) == 24
