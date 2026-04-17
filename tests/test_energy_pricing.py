"""Integracja energy_pricing z mockiem RCE (bez sieci)."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from energy_pricing import effective_import_pln_per_kwh, hourly_effective_import_pln_per_kwh_for_day
from tariff_g12 import G12TariffConfig


@pytest.fixture
def sample_tariff() -> G12TariffConfig:
    # Domyślne godziny nocne = ENEA G12 (w module); 15 = dzień, 22 = noc
    return G12TariffConfig(
        distribution_day_pln_per_kwh=0.35,
        distribution_night_pln_per_kwh=0.18,
        energy_from_rce=True,
    )


def test_effective_import_uses_injected_rce_hourly(
    sample_tariff: G12TariffConfig, tmp_path: Path
) -> None:
    hourly_rce = [0.5] * 24
    hourly_rce[15] = 1.0

    with patch(
        "energy_pricing.get_or_fetch_hourly_rce_pln_per_kwh",
        return_value=hourly_rce,
    ):
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
    with patch(
        "energy_pricing.get_or_fetch_hourly_rce_pln_per_kwh",
        return_value=[0.4] * 24,
    ):
        row = hourly_effective_import_pln_per_kwh_for_day(
            date(2026, 4, 1), tariff=sample_tariff
        )
    assert len(row) == 24
