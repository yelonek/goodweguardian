"""Orkiestracja: plan → audyt → rekonsyliacja → review."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime

from guardian_config import TELEMETRY_TZ
from planner.audit import append_audit, new_event
from planner.config import ensure_planner_dirs
from planner.inputs import build_hour_inputs, latest_soc_from_telemetry
from planner.models import DailyPlan
from planner.optimizer import optimize_horizon
from planner.plan_store import load_plan, save_plan
from planner.reconcile import reconcile_hour
from planner.review import build_day_review, save_review
from planner.telemetry import hourly_actuals
from energy_pricing import pricing_day_breakdown

log = logging.getLogger("planner")


def build_daily_plan(
    *,
    local_date: date | None = None,
    soc_start_pct: float | None = None,
) -> DailyPlan:
    """Buduje i zapisuje plan doby + wpis audytu ``plan_created``."""
    ensure_planner_dirs()
    d = local_date or date.today()
    start = datetime(d.year, d.month, d.day, 0, 0, 0)

    hour_inputs, snapshot = build_hour_inputs(start_dt=start)
    soc = soc_start_pct
    if soc is None:
        soc = latest_soc_from_telemetry(d) or 50.0

    opt = optimize_horizon(hour_inputs, soc_start_pct=soc)
    plan_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    plan = DailyPlan(
        plan_id=plan_id,
        local_date=d.isoformat(),
        generated_at=now.isoformat(),
        timezone=TELEMETRY_TZ,
        soc_start_pct=soc,
        expected_total_cashflow_pln=opt.total_cashflow_pln,
        optimizer="dp_net_kwh_v1",
        inputs_snapshot=snapshot,
        hours=opt.hours,
    )
    save_plan(plan)
    append_audit(
        new_event(
            local_date=d.isoformat(),
            kind="plan_created",
            plan_id=plan_id,
            payload={
                "expected_total_cashflow_pln": opt.total_cashflow_pln,
                "soc_start_pct": soc,
                "hours_count": len(opt.hours),
            },
        )
    )
    log.info(
        "plan %s for %s: expected %.2f PLN (%d hours)",
        plan_id[:8],
        d.isoformat(),
        opt.total_cashflow_pln,
        len(opt.hours),
    )
    return plan


def reconcile_day(local_date: date | None = None) -> int:
    """Rekonsyliuje każdą godzinę z telemetrią; zapisuje audyt ``hour_reconciled``."""
    d = local_date or date.today()
    plan = load_plan(d.isoformat())
    actuals = hourly_actuals(d)
    pricing = pricing_day_breakdown(d)
    n = 0
    for h in sorted(actuals.keys()):
        rec = reconcile_hour(
            local_date=d,
            hour=h,
            plan=plan,
            actuals=actuals,
            pricing=pricing,
        )
        append_audit(
            new_event(
                local_date=d.isoformat(),
                kind="hour_reconciled",
                plan_id=plan.plan_id if plan else None,
                payload=rec.model_dump(),
            )
        )
        n += 1
    log.info("reconciled %d hours for %s", n, d.isoformat())
    return n


def review_day(local_date: date | None = None) -> str:
    """Pełna retrospektywa doby — zapis review + audyt; zwraca tekst dla użytkownika."""
    d = local_date or date.today()
    review = build_day_review(d)
    save_review(review)
    append_audit(
        new_event(
            local_date=d.isoformat(),
            kind="day_reviewed",
            plan_id=review.plan_id,
            payload={
                "actual_total_cashflow_pln": review.actual_total_cashflow_pln,
                "perfect_foresight_cashflow_pln": review.perfect_foresight_cashflow_pln,
                "uplift_vs_actual_pln": review.uplift_vs_actual_pln,
                "recommendations": review.recommendations,
            },
        )
    )
    lines = [
        review.summary_pl,
        "",
        "Rekomendacje:",
    ]
    for i, r in enumerate(review.recommendations, 1):
        lines.append(f"  {i}. {r}")
    text = "\n".join(lines)
    log.info("review %s:\n%s", d.isoformat(), text)
    return text
