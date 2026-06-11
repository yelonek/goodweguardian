"""Orkiestracja: rolling plan + dzienny audyt."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime

from guardian_config import TELEMETRY_TZ
from planner.audit import append_audit, new_event
from planner.config import ensure_planner_dirs
from planner.day_audit import build_day_audit, save_day_audit
from planner.inputs import build_hour_inputs_for_slots, latest_soc_from_telemetry
from planner.models import DailyPlan
from planner.optimizer import optimize_horizon
from planner.plan_store import save_plan
from planner.pricing_horizon import local_now_naive, priced_horizon_slots, slot_to_local_iso
log = logging.getLogger("planner")


def build_rolling_plan(
    *,
    soc_start_pct: float | None = None,
    now: datetime | None = None,
) -> DailyPlan | None:
    """
    Rolling plan od bieżącej godziny do ostatniej z cenami (dziś + jutro gdy RCE jest).

    Zawsze liczony (niezależnie od przełącznika egzekucji w Guardianie).
    Zwraca ``None`` tylko gdy brak slotów z cennikiem.
    """
    ensure_planner_dirs()
    now_local = now or local_now_naive()
    slots = priced_horizon_slots(now=now_local)
    if not slots:
        log.warning("no priced horizon slots — skip plan")
        return None

    hour_inputs, snapshot = build_hour_inputs_for_slots(slots, now=now_local)
    soc = soc_start_pct
    if soc is None:
        soc = latest_soc_from_telemetry(now_local.date()) or 50.0

    opt = optimize_horizon(hour_inputs, soc_start_pct=soc)
    plan_id = str(uuid.uuid4())
    generated = datetime.now(UTC)
    anchor_date = now_local.date().isoformat()

    plan = DailyPlan(
        plan_id=plan_id,
        local_date=anchor_date,
        generated_at=generated.isoformat(),
        timezone=TELEMETRY_TZ,
        horizon_start=slot_to_local_iso(slots[0]),
        horizon_end=slot_to_local_iso(slots[-1]),
        soc_start_pct=soc,
        expected_total_cashflow_pln=opt.total_cashflow_pln,
        optimizer="lp_battery_v1",
        inputs_snapshot=snapshot,
        hours=opt.hours,
    )
    save_plan(plan)
    append_audit(
        new_event(
            local_date=anchor_date,
            kind="plan_created",
            plan_id=plan_id,
            payload={
                "expected_total_cashflow_pln": opt.total_cashflow_pln,
                "soc_start_pct": soc,
                "hours_count": len(opt.hours),
                "horizon_start": plan.horizon_start,
                "horizon_end": plan.horizon_end,
            },
        )
    )
    log.info(
        "plan %s %s→%s: expected %.2f PLN (%d h, soc %.1f%%)",
        plan_id[:8],
        plan.horizon_start,
        plan.horizon_end,
        opt.total_cashflow_pln,
        len(opt.hours),
        soc,
    )
    return plan


def build_daily_plan(
    *,
    local_date: date | None = None,
    soc_start_pct: float | None = None,
) -> DailyPlan | None:
    """Alias — rolling plan (``local_date`` ignorowany, zachowany dla kompatybilności API)."""
    _ = local_date
    return build_rolling_plan(soc_start_pct=soc_start_pct)


def audit_day(local_date: date | None = None) -> str:
    """Dzienny audyt: fakty vs perfect foresight; zapis ``audit_YYYY-MM-DD.json``."""
    d = local_date or date.today()
    audit = build_day_audit(d)
    save_day_audit(audit)
    append_audit(
        new_event(
            local_date=d.isoformat(),
            kind="day_audited",
            payload={
                "actual_total_cashflow_pln": audit.actual_total_cashflow_pln,
                "perfect_foresight_cashflow_pln": audit.perfect_foresight_cashflow_pln,
                "uplift_vs_actual_pln": audit.uplift_vs_actual_pln,
            },
        )
    )
    log.info("audit %s:\n%s", d.isoformat(), audit.summary_pl)
    return audit.summary_pl
