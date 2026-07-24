"""Jednolity system ustawień strojenia: jeden schemat Pydantic + jedno źródło prawdy.

Jedno źródło prawdy dla pokręteł strojenia to ``state/settings.json``:

  1. domyślne wartości schematu ``GuardianSettings`` (bezpieczna siatka startowa),
  2. ``state/settings.json`` → ``overrides`` (jedyne miejsce, gdzie trafiają zmiany z UI/onboardingu).

Dostęp przez ``get_settings()`` — wartość jest przeliczana po zmianie mtime pliku
``settings.json`` (bez restartu).

Czysty podział źródeł: każda zmienna jest ustawiana **albo** w ``.env`` (infrastruktura/
sekrety — IP inwertera, klucz API, proxy, ścieżki; patrz ``guardian_config``), **albo**
w ``settings.json`` przez dashboard (strojenie). Nigdy w obu — ``.env`` nie wpływa na
pola tego schematu.

Schemat obejmuje wyłącznie fine-tuning. Kontrakty egzekucji (§13), hold −1% i parametry
algorytmów nowcast/pv_correction są constami w kodzie.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import guardian_config as gc

logger = logging.getLogger("guardian")

# Kolejność i etykiety grup dla UI (data-driven).
GROUP_LABELS: dict[str, str] = {
    "flappy": "Soak / bilans godzinowy",
    "soc_full": "Obrona SOC — pełna bateria",
    "soc_low": "Obrona SOC — niska bateria",
    "night_reserve": "Rezerwa nocna SOC",
    "pricing": "Ceny i taryfa",
    "planner": "Planer / magazyn",
}
GROUP_ORDER: list[str] = list(GROUP_LABELS.keys())


def _meta(group: str, unit: str | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {"group": group}
    if unit is not None:
        extra["unit"] = unit
    return extra


class GuardianSettings(BaseModel):
    """Pokrętła fine-tuningu. Płaski model — grupowanie w UI przez metadane pola."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- Soak / koniec godziny ---
    soak_target_kwh: float = Field(
        0.1, ge=0.0, json_schema_extra=_meta("flappy", "kWh"),
        description="Dolny próg deadbandu soak — poniżej trzymamy drobny bufor.",
    )
    soak_trigger_kwh: float = Field(
        0.2, ge=0.0, json_schema_extra=_meta("flappy", "kWh"),
        description="Górny próg deadbandu soak — powyżej ładujemy bilans w dół do target.",
    )
    end_hour_window_s: int = Field(
        600, ge=0, le=3600, json_schema_extra=_meta("flappy", "s"),
        description="Okno na końcu godziny, w którym uruchamiany jest soak końcowy.",
    )
    end_hour_max_remaining_kwh: float = Field(
        0.2, ge=0.0, json_schema_extra=_meta("flappy", "kWh"),
        description="Docelowy maks. bilans na koniec godziny (soak końcowy dociąga w dół).",
    )

    # --- Obrona SOC pełna ---
    soc_full_defense_threshold_pct: float = Field(
        99.5, ge=0.0, le=100.0, json_schema_extra=_meta("soc_full", "%"),
        description="Próg SOC, od którego działa obrona pełnej baterii (blokada discharge).",
    )
    soc_full_defense_release_power_kw: float = Field(
        0.5, ge=0.0, json_schema_extra=_meta("soc_full", "kW"),
        description="Próg mocy bilansu, powyżej którego zwalniamy obronę pełną przy imporcie.",
    )
    soc_full_defense_carryover_minutes: int = Field(
        5, ge=1, le=30, json_schema_extra=_meta("soc_full", "min"),
        description="Pierwsze N minut nowej godziny z tarczą SOC jak po aktywności w poprzedniej.",
    )
    soc_full_defense_max_slot_min: int = Field(
        15, ge=1, le=59, json_schema_extra=_meta("soc_full", "min"),
        description="Maksymalna długość okna dla obrony pełnego SOC.",
    )

    # --- Obrona SOC niska (liniowy sufit mocy rozładowania) ---
    soc_low_cap_soc_high_pct: float = Field(
        20.0, ge=0.0, le=100.0, json_schema_extra=_meta("soc_low", "%"),
        description="Górny SOC strefy obrony (powyżej: brak limitu mocy).",
    )
    soc_low_cap_soc_low_pct: float = Field(
        10.0, ge=0.0, le=100.0, json_schema_extra=_meta("soc_low", "%"),
        description="Dolny SOC strefy obrony (poniżej: clamp do soc_low_cap_w_low).",
    )
    soc_low_cap_w_high: float = Field(
        1000.0, ge=0.0, json_schema_extra=_meta("soc_low", "W"),
        description="Sufit mocy discharge przy górnym SOC strefy.",
    )
    soc_low_cap_w_low: float = Field(
        70.0, ge=0.0, json_schema_extra=_meta("soc_low", "W"),
        description="Sufit mocy discharge przy dolnym SOC strefy.",
    )

    # --- Rezerwa nocna SOC ---
    soc_night_reserve_enabled: bool = Field(
        True, json_schema_extra=_meta("night_reserve"),
        description="Czy w godzinach nocnych blokować discharge poniżej progu rezerwy.",
    )
    soc_night_reserve_pct: float = Field(
        0.0, ge=0.0, le=100.0, json_schema_extra=_meta("night_reserve", "%"),
        description="Próg SOC rezerwy nocnej. 0 = wyłączone.",
    )
    soc_night_reserve_hours: list[int] = Field(
        default=[0, 1, 2, 3, 4, 5, 22, 23],
        json_schema_extra=_meta("night_reserve"),
        description="Godziny lokalne (0..23), w których działa rezerwa nocna.",
    )

    # --- Ceny i taryfa ---
    rce_export_multiplier: float = Field(
        1.23, ge=0.0, json_schema_extra=_meta("pricing"),
        description="Mnożnik RCE z PSE/proxy → stawka rozliczenia eksportu (np. VAT 1.23).",
    )
    tariff_distribution_day_pln_kwh: float = Field(
        0.0, ge=0.0, json_schema_extra=_meta("pricing", "PLN/kWh"),
        description="Składowa dystrybucji w strefie dziennej.",
    )
    tariff_distribution_night_pln_kwh: float = Field(
        0.0, ge=0.0, json_schema_extra=_meta("pricing", "PLN/kWh"),
        description="Składowa dystrybucji w strefie nocnej.",
    )
    tariff_energy_day_pln_kwh: float = Field(
        0.0, ge=0.0, json_schema_extra=_meta("pricing", "PLN/kWh"),
        description="Stała cena energii (sprzedawca) w strefie dziennej.",
    )
    tariff_energy_night_pln_kwh: float = Field(
        0.0, ge=0.0, json_schema_extra=_meta("pricing", "PLN/kWh"),
        description="Stała cena energii (sprzedawca) w strefie nocnej.",
    )

    # --- Planer / magazyn ---
    battery_capacity_kwh: float = Field(
        10.0, gt=0.0, json_schema_extra=_meta("planner", "kWh"),
        description="Pojemność magazynu — pacing export_profit oraz symulacja SOC planera.",
    )
    planner_policy_valid_minutes: int = Field(
        10, ge=1, le=1440, json_schema_extra=_meta("planner", "min"),
        description="Ważność artefaktu policy od computed_at (gdy brak valid_until).",
    )
    planner_battery_eta: float = Field(
        0.92, gt=0.0, le=1.0, json_schema_extra=_meta("planner"),
        description="Sprawność round-trip magazynu w symulacji planera.",
    )
    planner_battery_cycle_cost_pln: float = Field(
        0.10, ge=0.0, json_schema_extra=_meta("planner", "PLN/kWh"),
        description="Amortyzacja: PLN za kWh rozładowania magazynu.",
    )
    planner_soc_min_pct: float = Field(
        10.0, ge=0.0, le=100.0, json_schema_extra=_meta("planner", "%"),
        description="Minimalny SOC w symulacji planera / podłoga export_profit.",
    )
    planner_soc_max_pct: float = Field(
        100.0, ge=0.0, le=100.0, json_schema_extra=_meta("planner", "%"),
        description="Maksymalny SOC w symulacji planera.",
    )
    planner_load_lookback_days: int = Field(
        28, ge=1, le=180, json_schema_extra=_meta("planner", "dni"),
        description="Ile dni wstecz do próbek prognozy zużycia.",
    )
    planner_scenario_optimizer: bool = Field(
        True, json_schema_extra=_meta("planner"),
        description="Wieloscenariuszowy MILP (p10/p50/p90). Wyłączony = deterministyczny p50.",
    )
    planner_scenario_weight_pessimistic: float = Field(
        0.15, ge=0.0, json_schema_extra=_meta("planner"),
        description="Waga scenariusza pesymistycznego (normalizowana).",
    )
    planner_scenario_weight_base: float = Field(
        0.80, ge=0.0, json_schema_extra=_meta("planner"),
        description="Waga scenariusza bazowego (normalizowana).",
    )
    planner_scenario_weight_optimistic: float = Field(
        0.05, ge=0.0, json_schema_extra=_meta("planner"),
        description="Waga scenariusza optymistycznego (normalizowana).",
    )
    planner_soc_tracking: bool = Field(
        True, json_schema_extra=_meta("planner"),
        description=(
            "Tracking-SP: wspólna wizja SOC* + recourse ch/dis per scenariusz. "
            "Wyłączony = stary shared ch/dis (non-anticipativity na przepływach)."
        ),
    )
    planner_soc_tracking_lambda: float = Field(
        0.12,
        ge=0.0,
        json_schema_extra=_meta("planner", "PLN/kWh"),
        description=(
            "Kara |SOC_s − SOC*| w tracking-SP [PLN / kWh energii magazynu]. "
            "Za duże ≈ nadmierna ostrożność; za małe ≈ wizja jak p50."
        ),
    )
    pv_weather_correction_enabled: bool = Field(
        True,
        json_schema_extra=_meta("planner"),
        description=(
            "Korekta PV pogodą OWM (Free: clouds/pop/weather) na h+2…h+6. "
            "Wymaga OPENWEATHER_API_KEY + lat/lon w .env. Wyłączony = surowy Solcast od h+2."
        ),
    )


_FIELD_NAMES: frozenset[str] = frozenset(GuardianSettings.model_fields.keys())

# Warstwa bazowa = domyślne wartości schematu. To jedyny fallback, gdy pole nie ma
# override w settings.json. (``.env`` NIE jest warstwą runtime dla strojenia.)
_DEFAULTS: dict[str, Any] = GuardianSettings().model_dump()


# ---------------------------------------------------------------------------
# Persystencja: state/settings.json  { onboarding_completed, overrides }
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_CACHE: GuardianSettings | None = None
_CACHE_KEY: tuple[str, float | None] | None = None


def settings_path() -> Path:
    return Path(os.environ.get("GUARDIAN_SETTINGS_PATH") or (gc.STATE_DIR / "settings.json"))


def _read_raw() -> dict[str, Any]:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("settings.json read failed %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _overrides_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    ov = raw.get("overrides")
    if not isinstance(ov, dict):
        return {}
    return {k: v for k, v in ov.items() if k in _FIELD_NAMES}


def _build(overrides: dict[str, Any]) -> GuardianSettings:
    base = dict(_DEFAULTS)
    merged = {**base, **overrides}
    try:
        return GuardianSettings(**merged)
    except ValidationError as e:
        logger.warning("settings.json overrides invalid, dropping bad keys: %s", e)
        good: dict[str, Any] = {}
        for k, v in overrides.items():
            try:
                good[k] = GuardianSettings(**{**base, k: v}).model_dump()[k]
            except ValidationError:
                logger.warning("settings override skip key %s=%r", k, v)
        return GuardianSettings(**{**base, **good})


def reset_cache() -> None:
    """Wymuś ponowne przeliczenie przy następnym ``get_settings`` (testy / po zapisie)."""
    global _CACHE, _CACHE_KEY
    with _LOCK:
        _CACHE = None
        _CACHE_KEY = None


def get_settings() -> GuardianSettings:
    """Efektywne ustawienia (domyślne ∪ settings.json). Przeliczane po zmianie mtime pliku."""
    global _CACHE, _CACHE_KEY
    with _LOCK:
        path = settings_path()
        mtime = path.stat().st_mtime if path.exists() else None
        key = (str(path), mtime)
        if _CACHE is None or key != _CACHE_KEY:
            raw = _read_raw()
            _CACHE = _build(_overrides_from_raw(raw))
            _CACHE_KEY = key
        return _CACHE


# ---------------------------------------------------------------------------
# Zapis / onboarding
# ---------------------------------------------------------------------------

def is_onboarding_completed() -> bool:
    return bool(_read_raw().get("onboarding_completed", False))


def current_overrides() -> dict[str, Any]:
    return _overrides_from_raw(_read_raw())


def _validate_override(field: str, value: Any) -> Any:
    """Zwraca skoerowaną wartość pola lub rzuca ValidationError."""
    model = GuardianSettings(**{**_DEFAULTS, field: value})
    return model.model_dump()[field]


def _write_raw(raw: dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    reset_cache()


def update_overrides(
    updates: dict[str, Any],
    *,
    complete_onboarding: bool | None = None,
) -> GuardianSettings:
    """Merge do settings.json. Wartość ``None`` usuwa klucz (powrót do env/default).

    Klucze spoza schematu są ignorowane. Wartości poza zakresem odrzucają CAŁĄ operację.
    """
    raw = _read_raw()
    overrides = _overrides_from_raw(raw)
    for k, v in updates.items():
        if k not in _FIELD_NAMES:
            continue
        if v is None:
            overrides.pop(k, None)
            continue
        overrides[k] = _validate_override(k, v)
    raw["overrides"] = overrides
    if complete_onboarding is not None:
        raw["onboarding_completed"] = bool(complete_onboarding)
    _write_raw(raw)
    return get_settings()


def reset_overrides(keys: list[str] | None = None) -> GuardianSettings:
    """Usuń wskazane override (lub wszystkie). Zachowuje marker onboardingu."""
    raw = _read_raw()
    overrides = _overrides_from_raw(raw)
    if keys is None:
        overrides = {}
    else:
        for k in keys:
            overrides.pop(k, None)
    raw["overrides"] = overrides
    _write_raw(raw)
    return get_settings()


def sources() -> dict[str, str]:
    """Źródło każdego pola: ``override`` (z settings.json) albo ``default`` (schemat)."""
    ov = current_overrides()
    return {name: ("override" if name in ov else "default") for name in _FIELD_NAMES}


def settings_schema() -> dict[str, Any]:
    """JSON Schema modelu — do data-driven formularza w UI."""
    return GuardianSettings.model_json_schema()


def settings_api_payload() -> dict[str, Any]:
    """Payload dla GET /api/settings — schemat + wartości + źródła + stan onboardingu."""
    eff = get_settings()
    return {
        "onboarding_completed": is_onboarding_completed(),
        "schema": settings_schema(),
        "groups": [{"key": k, "label": GROUP_LABELS[k]} for k in GROUP_ORDER],
        "defaults": dict(_DEFAULTS),
        "effective": eff.model_dump(),
        "overrides": current_overrides(),
        "sources": sources(),
        "settings_path": str(settings_path()),
    }
