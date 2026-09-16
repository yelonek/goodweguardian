"""Wspólny rdzeń korekty mid-hour: jsonl, mix k, carry z poprzedniej godziny."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from guardian_config import TELEMETRY_DIR

DEFAULT_HORIZON_MIN = 60.0

log = logging.getLogger("planner")

KDetailFn = Callable[..., tuple[float | None, str, dict[str, Any]]]
EnergyInHourFn = Callable[..., tuple[float, int] | None]


def hour_elapsed_fraction(now: datetime) -> float:
    """α = ułamek bieżącej godziny lokalnej (0 na :00:00)."""
    return (now.minute + now.second / 60.0) / 60.0


def correction_spill_minutes(
    alpha: float,
    *,
    horizon_min: float = DEFAULT_HORIZON_MIN,
) -> float:
    """Ile minut h+1 dostaje k: horyzont minus reszta bieżącej godziny."""
    remaining_min = max(0.0, (1.0 - float(alpha)) * 60.0)
    return max(0.0, min(60.0, float(horizon_min) - remaining_min))


def mix_k_into_hour(
    *,
    f50: float,
    k: float,
    spill_min: float,
    q_raw: float | None = None,
) -> float:
    """``w · (k · F50) + (1−w) · q_raw``; ``w = spill_min / 60``. p50: ``q_raw`` puste."""
    w = max(0.0, min(1.0, float(spill_min) / 60.0))
    raw = float(f50 if q_raw is None else q_raw)
    nowcast = max(0.0, float(f50) * float(k))
    return max(0.0, w * nowcast + (1.0 - w) * max(0.0, raw))


def clip_k(value: float, *, k_min: float, k_max: float) -> float:
    return max(k_min, min(k_max, value))


def rate_blend_weight(
    alpha: float,
    *,
    blend_start: float,
    blend_end: float,
) -> float:
    """0 na początku godziny → 1 gdy alpha >= blend_end."""
    if blend_end <= blend_start:
        return 0.0
    if alpha <= blend_start:
        return 0.0
    if alpha >= blend_end:
        return 1.0
    return (alpha - blend_start) / (blend_end - blend_start)


def prev_local_hour(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


def _iter_hour_power_kw(
    *,
    local_date: str,
    hour: int,
    power_field: str,
    until_minute: int | None = None,
    from_minute: int | None = None,
    log_label: str,
) -> list[tuple[int, float]] | None:
    """Próbki ``(minute, kW)`` z jsonl. ``None`` gdy brak pliku / błąd odczytu."""
    path = TELEMETRY_DIR / f"telemetry_{local_date}.jsonl"
    target_hour = int(hour)
    if not path.exists():
        return None
    samples: list[tuple[int, float]] = []
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
                    if until_minute is not None and minute > until_minute:
                        continue
                    if from_minute is not None and minute < from_minute:
                        continue
                    samples.append((minute, float(row.get(power_field, 0.0)) / 1000.0))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError as e:
        log.debug("%s telemetry read failed %s: %s", log_label, path, e)
        return None
    return samples


def energy_in_hour(
    *,
    local_date: str,
    hour: int,
    power_field: str,
    until_minute: int | None = None,
    log_label: str = "intra",
) -> tuple[float, int] | None:
    """Energia [kWh] w godzinie lokalnej. ``until_minute`` włącznie; None = cała h."""
    samples = _iter_hour_power_kw(
        local_date=local_date,
        hour=hour,
        power_field=power_field,
        until_minute=until_minute,
        log_label=log_label,
    )
    if samples is None:
        return None
    if not samples:
        return None
    energy_kwh = sum(kw / 60.0 for _minute, kw in samples)
    return energy_kwh, len(samples)


def recent_average_kw(
    now: datetime,
    *,
    window_min: int,
    power_field: str,
    log_label: str = "intra",
) -> tuple[float, int] | None:
    """Średnia moc [kW] z ostatnich ``window_min`` minut bieżącej godziny."""
    if window_min <= 0:
        return None
    start_minute = max(0, now.minute - window_min)
    samples = _iter_hour_power_kw(
        local_date=now.date().isoformat(),
        hour=now.hour,
        power_field=power_field,
        until_minute=now.minute,
        from_minute=start_minute,
        log_label=log_label,
    )
    if samples is None or not samples:
        return None
    power_kw = [kw for _minute, kw in samples]
    return sum(power_kw) / len(power_kw), len(power_kw)


def minute_series_in_hour(
    now: datetime,
    *,
    power_field: str,
    kw_key: str,
    log_label: str = "intra",
) -> list[dict[str, float | int]]:
    """Minutowa kumulacja: ostatnia moc w minucie × 1/60, od :00."""
    samples = _iter_hour_power_kw(
        local_date=now.date().isoformat(),
        hour=now.hour,
        power_field=power_field,
        until_minute=now.minute,
        log_label=log_label,
    )
    if not samples:
        return []
    by_minute: dict[int, float] = {}
    for minute, kw in samples:
        by_minute[minute] = kw
    series: list[dict[str, float | int]] = []
    cum_kwh = 0.0
    for minute in sorted(by_minute):
        kw = by_minute[minute]
        cum_kwh += kw / 60.0
        series.append({"minute": minute, kw_key: kw, "cum_kwh": cum_kwh})
    return series


def compute_k_prev_hour(
    now: datetime,
    *,
    f50_prev_kwh: float,
    energy_in_hour_fn: EnergyInHourFn,
    k_detail_fn: KDetailFn,
    min_samples: int,
) -> tuple[float | None, dict[str, Any]]:
    """k = A_prev / F50_prev z pełnej poprzedniej godziny."""
    prev = prev_local_hour(now)
    meta: dict[str, Any] = {
        "f50_prev_kwh": float(f50_prev_kwh),
        "a_prev_kwh": None,
        "prev_samples": 0,
        "prev_date": prev.date().isoformat(),
        "prev_hour": prev.hour,
        "k_prev": None,
        "k_prev_reason": None,
        "k_prev_raw": None,
    }
    energy = energy_in_hour_fn(
        local_date=prev.date().isoformat(),
        hour=prev.hour,
    )
    if energy is None:
        meta["k_prev_reason"] = "no_telemetry"
        return None, meta
    a_prev, samples = energy
    meta["a_prev_kwh"] = a_prev
    meta["prev_samples"] = samples
    if samples < min_samples:
        meta["k_prev_reason"] = "too_few_samples"
        return None, meta
    alpha_prev = min(1.0, samples / 60.0)
    k_prev, reason, detail = k_detail_fn(
        f50_kwh=float(f50_prev_kwh),
        a_so_far_kwh=a_prev,
        alpha=alpha_prev,
    )
    meta["k_prev_reason"] = reason
    meta["k_prev"] = k_prev
    meta["k_prev_raw"] = detail.get("k_raw")
    return k_prev, meta
