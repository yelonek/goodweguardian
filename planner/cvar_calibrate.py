"""Automatyczna kalibracja PLANNER_CVAR_LAMBDA na telemetrii historycznej."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from economics import cashflow_pln_for_hour
from guardian_config import TELEMETRY_DIR
from planner.battery import BatteryParams
from planner import config as planner_cfg
from planner.inputs import build_hour_inputs_for_slots
from planner.risk_optimizer import optimize_horizon_cvar
from planner.telemetry import hourly_actuals
from planner.config import PLANNER_DIR, ensure_planner_dirs

log = logging.getLogger("planner")

CALIBRATION_CACHE_PATH = PLANNER_DIR / "cvar_calibration.json"
CALIBRATION_HISTORY_PATH = PLANNER_DIR / "cvar_calibration_history.jsonl"


def calibration_to_audit_payload(result: CvarCalibrationResult) -> dict:
    return {
        "lambda_value": result.lambda_value,
        "calibrated_at": result.calibrated_at,
        "lookback_days": result.lookback_days,
        "days_used": result.days_used,
        "scores_by_lambda": result.scores_by_lambda,
        "method": result.method,
        "notes": result.notes,
    }


def append_calibration_history(result: CvarCalibrationResult, *, source: str) -> None:
    """Append-only historia kalibracji λ — do analizy offline."""
    ensure_planner_dirs()
    row = {
        "ts_utc": datetime.now(UTC).isoformat(),
        "source": source,
        **calibration_to_audit_payload(result),
    }
    with CALIBRATION_HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def _audit_calibration(result: CvarCalibrationResult, *, source: str) -> None:
    from planner.audit import append_audit, new_event

    append_audit(
        new_event(
            local_date=datetime.now(UTC).date().isoformat(),
            kind="cvar_calibrated",
            payload={"source": source, **calibration_to_audit_payload(result)},
        )
    )


@dataclass(frozen=True)
class CvarCalibrationResult:
    lambda_value: float
    calibrated_at: str
    lookback_days: int
    days_used: int
    scores_by_lambda: dict[str, float]
    method: str
    notes: list[str]


def _parse_calibration_cache(raw: dict) -> CvarCalibrationResult | None:
    try:
        return CvarCalibrationResult(
            lambda_value=float(raw["lambda_value"]),
            calibrated_at=str(raw["calibrated_at"]),
            lookback_days=int(raw["lookback_days"]),
            days_used=int(raw["days_used"]),
            scores_by_lambda={str(k): float(v) for k, v in (raw.get("scores_by_lambda") or {}).items()},
            method=str(raw.get("method") or "backtest"),
            notes=list(raw.get("notes") or []),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _cache_fresh(result: CvarCalibrationResult, *, now: datetime) -> bool:
    try:
        ts = datetime.fromisoformat(result.calibrated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_h = (now - ts.astimezone(UTC)).total_seconds() / 3600.0
    return age_h < float(planner_cfg.PLANNER_CVAR_CALIBRATE_CACHE_HOURS)


def load_calibration_cache() -> CvarCalibrationResult | None:
    if not CALIBRATION_CACHE_PATH.exists():
        return None
    try:
        raw = json.loads(CALIBRATION_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return _parse_calibration_cache(raw)


def save_calibration_cache(result: CvarCalibrationResult) -> None:
    ensure_planner_dirs()
    payload = {
        "lambda_value": result.lambda_value,
        "calibrated_at": result.calibrated_at,
        "lookback_days": result.lookback_days,
        "days_used": result.days_used,
        "scores_by_lambda": result.scores_by_lambda,
        "method": result.method,
        "notes": result.notes,
    }
    CALIBRATION_CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _telemetry_dates_in_lookback(
    *,
    end_date: date,
    lookback_days: int,
) -> list[date]:
    out: list[date] = []
    for offset in range(1, lookback_days + 1):
        d = end_date - timedelta(days=offset)
        path = TELEMETRY_DIR / f"telemetry_{d.isoformat()}.jsonl"
        if path.exists():
            out.append(d)
    return out


def _simulate_plan_cashflow_pln(
    *,
    hours_in: list,
    hour_plans: list,
    actuals: dict[int, dict],
    soc_start_pct: float,
    params: BatteryParams,
) -> float | None:
    """Symulacja: planowany net (jak Flappy) × ceny — bez replay SOC."""
    _ = (soc_start_pct, params)
    total = 0.0
    matched = 0

    for hin, hp in zip(hours_in, hour_plans, strict=True):
        if int(hin.hour) not in actuals:
            continue
        matched += 1
        net = float(hp.target_net_kwh)
        total += cashflow_pln_for_hour(
            net,
            rce_pln_per_kwh=hin.export_pln_per_kwh,
            import_pln_per_kwh=hin.import_pln_per_kwh,
        )
        total -= float(hp.battery_wear_cost_pln)

    if matched < planner_cfg.PLANNER_CVAR_CALIBRATE_MIN_DAY_HOURS:
        return None
    return total


def _backtest_lambda_on_day(
    local_date: date,
    *,
    lambda_value: float,
    params: BatteryParams,
) -> float | None:
    actuals = hourly_actuals(local_date)
    if len(actuals) < planner_cfg.PLANNER_CVAR_CALIBRATE_MIN_DAY_HOURS:
        return None

    slots = [(local_date.isoformat(), h) for h in range(24)]
    day_start = datetime(local_date.year, local_date.month, local_date.day)
    hours_in, _ = build_hour_inputs_for_slots(slots, now=day_start)
    if not hours_in:
        return None

    first_h = min(actuals.keys())
    soc_start = float(actuals[first_h].get("last_soc_pct") or 50.0)

    opt = optimize_horizon_cvar(
        hours_in,
        soc_start_pct=soc_start,
        params=params,
        cvar_lambda=lambda_value,
    )
    return _simulate_plan_cashflow_pln(
        hours_in=hours_in,
        hour_plans=opt.hours,
        actuals=actuals,
        soc_start_pct=soc_start,
        params=params,
    )


def calibrate_cvar_lambda(
    *,
    end_date: date | None = None,
    lookback_days: int | None = None,
    grid: list[float] | None = None,
) -> CvarCalibrationResult:
    """
    Grid-search λ: maksymalizuj średni symulowany cashflow na dniach z telemetrią.

    Symulacja: plan z prognoz (jak o północy) + rzeczywiste PV/load z telemetrii.
    """
    end = end_date or date.today()
    lookback = lookback_days if lookback_days is not None else planner_cfg.PLANNER_CVAR_CALIBRATE_LOOKBACK_DAYS
    candidates = grid if grid is not None else list(planner_cfg.PLANNER_CVAR_CALIBRATE_GRID)
    if not candidates:
        candidates = [0.0, 1.0]

    bp = BatteryParams()
    dates = _telemetry_dates_in_lookback(end_date=end, lookback_days=lookback)
    notes: list[str] = []

    scores: dict[float, list[float]] = {lam: [] for lam in candidates}
    for d in dates:
        for lam in candidates:
            cf = _backtest_lambda_on_day(d, lambda_value=lam, params=bp)
            if cf is not None:
                scores[lam].append(cf)

    mean_scores: dict[float, float] = {}
    for lam, vals in scores.items():
        if vals:
            mean_scores[lam] = sum(vals) / len(vals)

    days_used = max((len(v) for v in scores.values()), default=0)

    if days_used < planner_cfg.PLANNER_CVAR_CALIBRATE_MIN_DAYS or not mean_scores:
        fallback = float(planner_cfg.PLANNER_CVAR_CALIBRATE_DEFAULT_LAMBDA)
        notes.append(
            f"Za mało dni backtestu ({days_used}<{planner_cfg.PLANNER_CVAR_CALIBRATE_MIN_DAYS}) "
            f"— λ={fallback:.2f} domyślne."
        )
        return CvarCalibrationResult(
            lambda_value=fallback,
            calibrated_at=datetime.now(UTC).isoformat(),
            lookback_days=lookback,
            days_used=days_used,
            scores_by_lambda={str(k): v for k, v in mean_scores.items()},
            method="fallback",
            notes=notes,
        )

    best_lam = max(mean_scores.keys(), key=lambda lam: (mean_scores[lam], -lam))
    best_score = mean_scores[best_lam]
    base_score = mean_scores.get(0.0)
    if base_score is not None and best_lam > 0.0:
        notes.append(
            f"Wybrane λ={best_lam:g} (śr. {best_score:+.2f} PLN/d) vs λ=0 ({base_score:+.2f} PLN/d)."
        )
    else:
        notes.append(f"Wybrane λ={best_lam:g} (śr. {best_score:+.2f} PLN/d).")

    return CvarCalibrationResult(
        lambda_value=float(best_lam),
        calibrated_at=datetime.now(UTC).isoformat(),
        lookback_days=lookback,
        days_used=days_used,
        scores_by_lambda={str(k): v for k, v in sorted(mean_scores.items())},
        method="backtest_mean_cashflow",
        notes=notes,
    )


def get_effective_cvar_lambda(*, force_refresh: bool = False) -> float:
    """Zwraca λ do optymalizatora (stałe z env, auto z cache lub świeża kalibracja)."""
    from planner.config import cvar_lambda_mode, fixed_cvar_lambda_value

    mode = cvar_lambda_mode()
    if mode == "fixed":
        return fixed_cvar_lambda_value()
    if mode != "auto":
        return 0.0

    now = datetime.now(UTC)
    if not force_refresh:
        cached = load_calibration_cache()
        if cached is not None and _cache_fresh(cached, now=now):
            log.info(
                "CVaR λ from cache: %.3f (calibrated_at=%s days=%d)",
                cached.lambda_value,
                cached.calibrated_at,
                cached.days_used,
            )
            return cached.lambda_value

    result = calibrate_cvar_lambda()
    save_calibration_cache(result)
    append_calibration_history(result, source="auto_refresh")
    _audit_calibration(result, source="auto_refresh")
    log.info(
        "CVaR λ auto-calibrated: %.3f (days=%d method=%s scores=%s)",
        result.lambda_value,
        result.days_used,
        result.method,
        result.scores_by_lambda,
    )
    for note in result.notes:
        log.info("CVaR calibrate: %s", note)
    return result.lambda_value


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Kalibracja PLANNER_CVAR_LAMBDA na telemetrii")
    parser.add_argument("--refresh", action="store_true", help="wymuś ponowną kalibrację")
    parser.add_argument("--lookback", type=int, default=None)
    args = parser.parse_args()

    if args.refresh or not load_calibration_cache():
        result = calibrate_cvar_lambda(lookback_days=args.lookback)
        save_calibration_cache(result)
        append_calibration_history(result, source="cli_refresh" if args.refresh else "cli_init")
        _audit_calibration(result, source="cli")
    else:
        result = load_calibration_cache()
        assert result is not None

    print(f"lambda={result.lambda_value}")
    print(f"days_used={result.days_used} method={result.method}")
    for k, v in sorted(result.scores_by_lambda.items(), key=lambda x: float(x[0])):
        print(f"  λ={k}: mean_sim_cf={v:+.3f} PLN/d")
    for note in result.notes:
        print(f"note: {note}")
