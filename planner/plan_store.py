"""Zapis planów: najnowszy wskaźnik + niezmienna historia (każdy przebieg planera)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from guardian_config import TELEMETRY_TZ
from planner.audit import read_audit_events
from planner.config import PLANNER_PLANS_DIR, PLANNER_PLANS_HISTORY_DIR, ensure_planner_dirs
from planner.models import DailyPlan, HourPlan


def plan_latest_path(local_date: str) -> Path:
    ensure_planner_dirs()
    return PLANNER_PLANS_DIR / f"plan_{local_date}.json"


def plan_history_path(plan_id: str) -> Path:
    ensure_planner_dirs()
    return PLANNER_PLANS_HISTORY_DIR / f"plan_{plan_id}.json"


def save_plan(plan: DailyPlan) -> Path:
    """
    Zapisuje niezmienny snapshot w historii i aktualizuje wskaźnik ``plan_{date}.json``.
    """
    ensure_planner_dirs()
    hist = plan_history_path(plan.plan_id)
    hist.write_text(
        json.dumps(plan.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    latest = plan_latest_path(plan.local_date)
    latest.write_text(
        json.dumps(plan.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return hist


def load_plan(local_date: str) -> DailyPlan | None:
    """Ostatni plan na daną dobę (do dashboardu / bieżącego horyzontu)."""
    path = plan_latest_path(local_date)
    if not path.exists():
        return None
    return DailyPlan.model_validate_json(path.read_text(encoding="utf-8"))


def load_plan_by_id(plan_id: str) -> DailyPlan | None:
    path = plan_history_path(plan_id)
    if not path.exists():
        return None
    return DailyPlan.model_validate_json(path.read_text(encoding="utf-8"))


def list_plan_ids_for_date(local_date: str) -> list[str]:
    """Wszystkie plan_id z historii dla doby (posortowane po ``generated_at``)."""
    d = local_date
    prefix = f"plan_{d}"  # not used - history uses uuid
    plans: list[tuple[str, str]] = []
    hist_dir = PLANNER_PLANS_HISTORY_DIR
    if not hist_dir.exists():
        return []
    for path in hist_dir.glob("plan_*.json"):
        try:
            p = DailyPlan.model_validate_json(path.read_text(encoding="utf-8"))
            if p.local_date == d:
                plans.append((p.generated_at, p.plan_id))
        except Exception:
            continue
    plans.sort(key=lambda x: x[0])
    return [pid for _, pid in plans]


def _hour_start_utc(local_date: date, hour: int) -> datetime:
    tz = ZoneInfo(TELEMETRY_TZ)
    local = datetime(local_date.year, local_date.month, local_date.day, hour, 0, 0, tzinfo=tz)
    return local.astimezone(UTC)


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def plan_effective_at(local_date: date, hour: int) -> DailyPlan | None:
    """
    Plan obowiązujący tuż przed startem godziny ``hour`` (ostatni ``plan_created`` przed :00).

    Do audytu: porównujemy z tym, co system „wiedział” przed godziną, nie z planem z wieczora.
    """
    hour_start = _hour_start_utc(local_date, hour)
    d_iso = local_date.isoformat()

    best_id: str | None = None
    for ev in read_audit_events(d_iso):
        if ev.kind != "plan_created" or not ev.plan_id:
            continue
        if _parse_utc(ev.ts_utc) <= hour_start:
            best_id = ev.plan_id

    if best_id:
        loaded = load_plan_by_id(best_id)
        if loaded is not None:
            return loaded

    # fallback: historia bez audytu (starsze uruchomienia)
    for pid in reversed(list_plan_ids_for_date(d_iso)):
        loaded = load_plan_by_id(pid)
        if loaded is None:
            continue
        if _parse_utc(loaded.generated_at) <= hour_start:
            return loaded

    return None


def hour_plan_from(plan: DailyPlan | None, local_date: date, hour: int) -> HourPlan | None:
    if plan is None:
        return None
    d_iso = local_date.isoformat()
    for hp in plan.hours:
        if hp.date == d_iso and hp.hour == hour:
            return hp
    return None
