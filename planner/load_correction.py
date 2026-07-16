"""Zwężanie pasm load mid-hour — analogia do ``pv_remainder_bands_kwh``."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from guardian_config import TELEMETRY_DIR
from planner.pv_correction import hour_elapsed_fraction

# Kill-switch i tempo — const jak PV_BAND_*.
LOAD_BAND_NARROW_ENABLED = True
LOAD_BAND_RATE_WINDOW_MIN = 15
LOAD_BAND_RATE_P25_FACTOR = 0.70
LOAD_BAND_RATE_P75_FACTOR = 1.15
LOAD_BAND_RATE_MIN_ALPHA = 0.15

log = logging.getLogger("planner")


def load_recent_average_kw(
    now: datetime,
    *,
    window_min: int = LOAD_BAND_RATE_WINDOW_MIN,
) -> tuple[float, int] | None:
    """Średnia moc load [kW] z ostatnich ``window_min`` minut bieżącej godziny."""
    if window_min <= 0:
        return None
    path = TELEMETRY_DIR / f"telemetry_{now.date().isoformat()}.jsonl"
    target_hour = now.hour
    start_minute = max(0, now.minute - window_min)
    power_kw: list[float] = []

    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if int(row["local_hour"]) != target_hour:
                        continue
                    minute = int(row.get("local_minute", 0))
                    if minute > now.minute or minute < start_minute:
                        continue
                    power_kw.append(float(row.get("consumption_w", 0.0)) / 1000.0)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError as e:
        log.debug("load recent average read failed %s: %s", path, e)
        return None

    if not power_kw:
        return None
    return sum(power_kw) / len(power_kw), len(power_kw)


def load_energy_so_far_in_hour(now: datetime) -> tuple[float, int] | None:
    """
    Energia load [kWh] od początku bieżącej godziny lokalnej.

    Zwraca ``(energia, liczba_próbek)`` lub ``None`` gdy brak telemetrii.
    """
    path = TELEMETRY_DIR / f"telemetry_{now.date().isoformat()}.jsonl"
    target_hour = now.hour
    energy_kwh = 0.0
    count = 0

    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if int(row["local_hour"]) != target_hour:
                        continue
                    minute = int(row.get("local_minute", 0))
                    if minute > now.minute:
                        continue
                    energy_kwh += float(row.get("consumption_w", 0.0)) / 1000.0 / 60.0
                    count += 1
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError as e:
        log.debug("load energy read failed %s: %s", path, e)
        return None

    if count == 0:
        return None
    return energy_kwh, count


def load_remainder_bands_kwh(
    *,
    p50_full: float,
    p25_full: float,
    p75_full: float,
    a_so_far: float,
    alpha: float,
    recent_kw: float | None = None,
    narrow_enabled: bool | None = None,
) -> tuple[float, float, float]:
    """
    Pasma load [kWh] na **resztę** bieżącej godziny (p50, p25, p75).

    Niepewność zwęża się z ``(1 − α)``; ``P*_total ≥ A_so_far``; opcjonalny
    floor reszty z ``recent_kw`` gdy dom nadal ciągnie.
    """
    if narrow_enabled is None:
        narrow_enabled = LOAD_BAND_NARROW_ENABLED

    p50_f = max(0.0, float(p50_full))
    p25_f = max(0.0, float(p25_full))
    p75_f = max(0.0, float(p75_full))
    a = max(0.0, float(a_so_far))
    al = max(0.0, min(1.0, float(alpha)))

    if not narrow_enabled:
        return (
            max(0.0, p50_f - a),
            max(0.0, p25_f - a),
            max(0.0, p75_f - a),
        )

    p25_tot = max(p25_f, a)
    p50_tot = max(p50_f, a)
    p75_tot = max(p75_f, a)
    width_tot = max(0.0, p75_tot - p25_tot)

    u = max(0.0, 1.0 - al)
    p50_rem = max(0.0, p50_tot - a)
    half = 0.5 * width_tot * u
    p25_rem = max(0.0, p50_rem - half)
    p75_rem = p50_rem + half

    if (
        recent_kw is not None
        and float(recent_kw) > 0.0
        and al >= LOAD_BAND_RATE_MIN_ALPHA
    ):
        frac = max(0.0, 1.0 - al)
        rate_p25 = float(recent_kw) * frac * LOAD_BAND_RATE_P25_FACTOR
        rate_p75 = float(recent_kw) * frac * LOAD_BAND_RATE_P75_FACTOR
        p25_rem = max(p25_rem, rate_p25)
        p75_rem = max(p75_rem, p25_rem, rate_p75)

    p25_rem = min(p25_rem, p50_rem)
    p75_rem = max(p75_rem, p50_rem)
    return p50_rem, p25_rem, p75_rem


def build_load_intra_meta(now: datetime) -> dict[str, Any]:
    """Meta load so-far / tempo dla mid-hour (snapshot / hour_remainder)."""
    alpha = hour_elapsed_fraction(now)
    energy = load_energy_so_far_in_hour(now)
    a_so_far = float(energy[0]) if energy is not None else None
    samples = int(energy[1]) if energy is not None else 0
    recent = load_recent_average_kw(now)
    recent_kw = float(recent[0]) if recent is not None else None
    recent_samples = int(recent[1]) if recent is not None else 0
    return {
        "band_narrow_enabled": LOAD_BAND_NARROW_ENABLED,
        "alpha": alpha,
        "a_so_far_kwh": a_so_far,
        "telemetry_samples": samples,
        "recent_kw": recent_kw,
        "recent_samples": recent_samples,
    }
