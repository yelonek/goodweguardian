"""Forecast zuzycia domu (baseline) na podstawie telemetrii."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from guardian_config import TELEMETRY_DIR


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(len(sorted_vals) - 1, lo + 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _hourly_kwh_from_file(path: Path) -> dict[int, float]:
    """
    Przybliza zuzycie godzinowe jako srednia consumption_w w godzinie / 1000.
    """
    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    hour = int(row["local_hour"])
                    val = float(row["consumption_w"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if 0 <= hour <= 23:
                    buckets[hour].append(val)
    except OSError:
        return {}

    out: dict[int, float] = {}
    for h in range(24):
        vals = buckets[h]
        if vals:
            out[h] = (sum(vals) / len(vals)) / 1000.0
    return out


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _historical_hour_samples(
    *,
    target_hour: int,
    target_weekend: bool,
    start_date: date,
    lookback_days: int,
) -> tuple[list[float], list[float], list[float]]:
    """
    Zwraca probki: (same_type, any_type_same_hour, all_hours_all_days).
    """
    same_type: list[float] = []
    any_type_same_hour: list[float] = []
    all_vals: list[float] = []
    for i in range(1, lookback_days + 1):
        d = start_date - timedelta(days=i)
        p = TELEMETRY_DIR / f"telemetry_{d.isoformat()}.jsonl"
        if not p.exists():
            continue
        hourly = _hourly_kwh_from_file(p)
        for v in hourly.values():
            all_vals.append(v)
        hv = hourly.get(target_hour)
        if hv is None:
            continue
        any_type_same_hour.append(hv)
        if _is_weekend(d) == target_weekend:
            same_type.append(hv)
    return same_type, any_type_same_hour, all_vals


def _historical_hour_samples_cached(
    *,
    target_hour: int,
    target_weekend: bool,
    target_date: date,
    lookback_days: int,
    cache: dict[date, dict[int, float]],
) -> tuple[list[float], list[float], list[float]]:
    same_type: list[float] = []
    any_type_same_hour: list[float] = []
    all_vals: list[float] = []
    for i in range(1, lookback_days + 1):
        d = target_date - timedelta(days=i)
        hourly = cache.get(d)
        if not hourly:
            continue
        for v in hourly.values():
            all_vals.append(v)
        hv = hourly.get(target_hour)
        if hv is None:
            continue
        any_type_same_hour.append(hv)
        if _is_weekend(d) == target_weekend:
            same_type.append(hv)
    return same_type, any_type_same_hour, all_vals


def build_daily_hourly_kwh_cache() -> dict[date, dict[int, float]]:
    """Jeden plik JSONL na dzien -> slownik godzina -> przyblizone kWh (srednia W/1000)."""
    out: dict[date, dict[int, float]] = {}
    for p in sorted(TELEMETRY_DIR.glob("telemetry_*.jsonl")):
        if not p.is_file():
            continue
        stem = p.stem
        if not stem.startswith("telemetry_"):
            continue
        try:
            d = date.fromisoformat(stem.removeprefix("telemetry_"))
        except ValueError:
            continue
        hourly = _hourly_kwh_from_file(p)
        if hourly:
            out[d] = hourly
    return out


def predict_load_one_hour(
    target_date: date,
    target_hour: int,
    lookback_days: int,
    cache: dict[date, dict[int, float]],
) -> dict[str, Any]:
    """
    Ta sama logika co pojedyncza godzina w forecast_load_hours, ale na cache (szybkie).
    Dni < target_date w cache; dzien docelowy nie jest w probce (jak w produkcji).
    """
    same_type, any_type_same_hour, all_vals = _historical_hour_samples_cached(
        target_hour=target_hour,
        target_weekend=_is_weekend(target_date),
        target_date=target_date,
        lookback_days=lookback_days,
        cache=cache,
    )
    samples = same_type if len(same_type) >= 5 else any_type_same_hour
    source = "weekday_weekend_hour"
    if len(samples) < 3:
        samples = all_vals
        source = "global_fallback"
    if not samples:
        return {
            "load_kwh_p25": 0.0,
            "load_kwh_p50": 0.0,
            "load_kwh_p75": 0.0,
            "samples": 0,
            "source": "no_history",
        }
    s = sorted(samples)
    return {
        "load_kwh_p25": _percentile(s, 0.25),
        "load_kwh_p50": median(s),
        "load_kwh_p75": _percentile(s, 0.75),
        "samples": len(samples),
        "source": source,
    }


def run_load_forecast_backtest(
    *,
    lookback_days: int = 28,
    max_days: int | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    """
    Leave-one-day-out: dla kazdej godziny prognoza uzywa wylacznie dni < D (jak API).
    Porownanie: actual kWh z telemetrii vs p50; coverage miedzy p25 a p75.
    """
    cache = build_daily_hourly_kwh_cache()
    dates = sorted(cache.keys())
    if not dates:
        return {
            "error": "brak plikow telemetry_*.jsonl",
            "lookback_days": lookback_days,
            "points": 0,
        }
    if max_days is not None:
        dates = dates[-max_days:]

    date_iter: Any = dates
    if progress:
        try:
            from tqdm import tqdm

            date_iter = tqdm(dates, desc="load forecast backtest")
        except ImportError:
            pass

    abs_err = 0.0
    abs_pct_sum = 0.0
    pct_count = 0
    in_band = 0
    n_points = 0
    wd_abs = 0.0
    wd_n = 0
    we_abs = 0.0
    we_n = 0
    skipped_no_actual = 0

    for d in date_iter:
        hourly = cache[d]
        for h in range(24):
            if h not in hourly:
                skipped_no_actual += 1
                continue
            actual = hourly[h]
            pred = predict_load_one_hour(d, h, lookback_days, cache)
            p50 = float(pred["load_kwh_p50"])
            p25 = float(pred["load_kwh_p25"])
            p75 = float(pred["load_kwh_p75"])
            err = abs(actual - p50)
            abs_err += err
            n_points += 1
            if actual >= 0.05:
                abs_pct_sum += (err / actual) * 100.0
                pct_count += 1
            if p25 <= actual <= p75:
                in_band += 1
            if _is_weekend(d):
                we_abs += err
                we_n += 1
            else:
                wd_abs += err
                wd_n += 1

    return {
        "lookback_days": lookback_days,
        "max_days_applied": max_days,
        "telemetry_days_in_cache": len(cache),
        "days_evaluated": len(dates),
        "points": n_points,
        "skipped_hours_no_telemetry": skipped_no_actual,
        "mae_kwh": (abs_err / n_points) if n_points else None,
        "mape_pct": (abs_pct_sum / pct_count) if pct_count else None,
        "mape_note": "MAPE tylko gdzie actual >= 0.05 kWh (unikaj dzielenia przez ~0).",
        "coverage_p25_p75": (in_band / n_points) if n_points else None,
        "weekday": {
            "mae_kwh": (wd_abs / wd_n) if wd_n else None,
            "points": wd_n,
        },
        "weekend": {
            "mae_kwh": (we_abs / we_n) if we_n else None,
            "points": we_n,
        },
    }


def forecast_load_hours(
    *,
    start_dt: datetime | None = None,
    hours: int = 24,
    lookback_days: int = 28,
) -> dict:
    """
    Baseline forecast: mediana historyczna per godzina, split weekday/weekend.
    """
    now = start_dt or datetime.now()
    rows: list[dict] = []
    for step in range(max(1, hours)):
        dt = now + timedelta(hours=step)
        target_date = dt.date()
        target_hour = dt.hour
        same_type, any_type_same_hour, all_vals = _historical_hour_samples(
            target_hour=target_hour,
            target_weekend=_is_weekend(target_date),
            start_date=target_date,
            lookback_days=lookback_days,
        )
        samples = same_type if len(same_type) >= 5 else any_type_same_hour
        source = "weekday_weekend_hour"
        if len(samples) < 3:
            samples = all_vals
            source = "global_fallback"
        if not samples:
            # Bez historii: konserwatywne zero.
            rows.append(
                {
                    "date": target_date.isoformat(),
                    "hour": target_hour,
                    "load_kwh_p25": 0.0,
                    "load_kwh_p50": 0.0,
                    "load_kwh_p75": 0.0,
                    "samples": 0,
                    "source": "no_history",
                }
            )
            continue
        s = sorted(samples)
        rows.append(
            {
                "date": target_date.isoformat(),
                "hour": target_hour,
                "load_kwh_p25": _percentile(s, 0.25),
                "load_kwh_p50": median(s),
                "load_kwh_p75": _percentile(s, 0.75),
                "samples": len(samples),
                "source": source,
            }
        )
    return {
        "generated_at": now.isoformat(),
        "lookback_days": lookback_days,
        "hours": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest baseline load forecast (telemetria)")
    parser.add_argument(
        "--lookback",
        type=int,
        default=28,
        help="dni wstecz do probek (jak w API)",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=None,
        help="ogranicz ostatnie N dni (domyslnie: wszystkie w cache)",
    )
    args = parser.parse_args()
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None  # type: ignore[misc, assignment]

    result = run_load_forecast_backtest(
        lookback_days=args.lookback,
        max_days=args.max_days,
        progress=bool(tqdm),
    )
    out = json.dumps(result, indent=2, ensure_ascii=False)
    if tqdm:
        tqdm.write(out)
    else:
        print(out)


if __name__ == "__main__":
    main()
