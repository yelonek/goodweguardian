"""Taryfa G12 — strefy i składanie kosztu."""

import pytest

from tariff_g12 import ENEA_G12_NIGHT_HOURS, G12TariffConfig, g12_tariff_from_env


def test_zone_for_hour() -> None:
    cfg = G12TariffConfig(
        night_hours=frozenset({0, 1, 22, 23}),
        distribution_day_pln_per_kwh=0.5,
        distribution_night_pln_per_kwh=0.2,
        energy_from_rce=True,
    )
    assert cfg.zone_for_hour(22) == "night"
    assert cfg.zone_for_hour(12) == "day"


def test_effective_with_rce() -> None:
    cfg = G12TariffConfig(
        night_hours=frozenset({23}),
        distribution_day_pln_per_kwh=0.4,
        distribution_night_pln_per_kwh=0.1,
        energy_from_rce=True,
    )
    # godz. 10 dzień: 0.4 + 0.5 = 0.9
    assert cfg.effective_import_pln_per_kwh(10, 0.5) == pytest.approx(0.9)
    # godz. 23 noc: 0.1 + 0.5 = 0.6
    assert cfg.effective_import_pln_per_kwh(23, 0.5) == pytest.approx(0.6)


def test_enaea_g12_night_hours_shape() -> None:
    assert len(ENEA_G12_NIGHT_HOURS) == 10
    assert {0, 1, 2, 3, 4, 5, 13, 14, 22, 23} == set(ENEA_G12_NIGHT_HOURS)


def test_g12_default_night_is_enaea(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TARIFF_G12_NIGHT_HOURS", raising=False)
    cfg = G12TariffConfig()
    assert cfg.night_hours == ENEA_G12_NIGHT_HOURS


def test_g12_tariff_from_env_ignores_obsolete_night_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TARIFF_G12_NIGHT_HOURS", raising=False)
    monkeypatch.setenv("TARIFF_G12_NIGHT_HOURS", "10,11,12")
    cfg = g12_tariff_from_env()
    assert cfg.night_hours == ENEA_G12_NIGHT_HOURS


def test_effective_fixed_energy() -> None:
    cfg = G12TariffConfig(
        night_hours=frozenset({23}),
        distribution_day_pln_per_kwh=0.4,
        distribution_night_pln_per_kwh=0.1,
        energy_from_rce=False,
        energy_day_pln_per_kwh=0.6,
        energy_night_pln_per_kwh=0.3,
    )
    assert cfg.effective_import_pln_per_kwh(10, 999.0) == pytest.approx(1.0)
    assert cfg.effective_import_pln_per_kwh(23, 999.0) == pytest.approx(0.4)
