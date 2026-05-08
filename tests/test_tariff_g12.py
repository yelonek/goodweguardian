"""Taryfa G12 — strefy i składanie kosztu."""

import pytest

from tariff_g12 import ENEA_G12_NIGHT_HOURS, G12TariffConfig, g12_tariff_from_env


def test_zone_for_hour() -> None:
    cfg = G12TariffConfig(
        night_hours=frozenset({0, 1, 22, 23}),
        distribution_day_pln_per_kwh=0.5,
        distribution_night_pln_per_kwh=0.2,
    )
    assert cfg.zone_for_hour(22) == "night"
    assert cfg.zone_for_hour(12) == "day"


def test_import_tariff_fixed_energy_by_zone() -> None:
    cfg = G12TariffConfig(
        night_hours=frozenset({23}),
        distribution_day_pln_per_kwh=0.4,
        distribution_night_pln_per_kwh=0.1,
        energy_day_pln_per_kwh=0.5,
        energy_night_pln_per_kwh=0.5,
    )
    assert cfg.import_pln_per_kwh(10) == pytest.approx(0.9)
    assert cfg.import_pln_per_kwh(23) == pytest.approx(0.6)


def test_import_tariff_different_energy_day_night() -> None:
    cfg = G12TariffConfig(
        night_hours=frozenset({23}),
        distribution_day_pln_per_kwh=0.4,
        distribution_night_pln_per_kwh=0.1,
        energy_day_pln_per_kwh=0.6,
        energy_night_pln_per_kwh=0.3,
    )
    assert cfg.import_pln_per_kwh(10) == pytest.approx(1.0)
    assert cfg.import_pln_per_kwh(23) == pytest.approx(0.4)


def test_effective_import_alias_ignores_rce() -> None:
    cfg = G12TariffConfig(
        night_hours=frozenset({23}),
        distribution_day_pln_per_kwh=0.4,
        distribution_night_pln_per_kwh=0.1,
        energy_day_pln_per_kwh=0.6,
        energy_night_pln_per_kwh=0.3,
    )
    assert cfg.effective_import_pln_per_kwh(10, 999.0) == pytest.approx(1.0)
    assert cfg.effective_import_pln_per_kwh(23, 999.0) == pytest.approx(0.4)


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
