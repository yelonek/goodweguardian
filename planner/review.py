"""Codzienna retrospektywa — „co możemy poprawić?”."""

from __future__ import annotations

from datetime import UTC, date, datetime

from economics import total_cashflow_pln_for_horizon
from energy_pricing import pricing_day_breakdown
from planner.config import PLANNER_REVIEWS_DIR, ensure_planner_dirs
from planner.models import DayReview, HourInputs
from planner.optimizer import optimize_horizon
from planner.plan_store import load_plan
from planner.reconcile import reconcile_hour
from planner.telemetry import hourly_actuals


def _recommendations(hours: list, actual_total: float, perfect: float | None, planned: float | None) -> list[str]:
    rec: list[str] = []
    if perfect is not None and perfect - actual_total > 0.5:
        rec.append(
            f"Potencjał arbitrażu magazynu/cen: ~{perfect - actual_total:.2f} PLN/dobę "
            "(perfect foresight na rzeczywistych PV/load)."
        )

    bad_hours = [h for h in hours if h.gap_vs_optimal_pln and h.gap_vs_optimal_pln > 0.2]
    if bad_hours:
        hs = ", ".join(f"{h.hour:02d}" for h in sorted(bad_hours, key=lambda x: -x.gap_vs_optimal_pln)[:5])
        rec.append(f"Godziny z największą stratą vs optimum: {hs}.")

    load_err = [
        h
        for h in hours
        if h.forecast_load_kwh is not None
        and h.actual_load_kwh is not None
        and abs(h.actual_load_kwh - h.forecast_load_kwh) > 0.4
    ]
    if len(load_err) >= 3:
        rec.append("Prognoza zużycia systematycznie myli — rozważ krótszy nowcast lub kalibrację baseline.")

    plan_miss = [
        h
        for h in hours
        if h.planned_net_kwh is not None
        and h.actual_net_kwh is not None
        and abs(h.actual_net_kwh - h.planned_net_kwh) > 0.25
    ]
    if len(plan_miss) >= 4:
        rec.append(
            "Guardian często nie trzyma planu netto — sprawdź slot eco, watchdog i limity SOC."
        )

    if not rec:
        rec.append("Dzień bliski optimum przy dostępnych danych; monitoruj kolejne doby.")
    return rec


def build_day_review(local_date: date) -> DayReview:
    plan = load_plan(local_date.isoformat())
    actuals = hourly_actuals(local_date)
    pricing = pricing_day_breakdown(local_date)

    hour_rows = [
        reconcile_hour(
            local_date=local_date,
            hour=h,
            plan=plan,
            actuals=actuals,
            pricing=pricing,
        )
        for h in range(24)
        if h in actuals
    ]

    cf_pairs: list[tuple[float, float, float]] = []
    perfect_inputs: list[HourInputs] = []
    for h in sorted(actuals.keys()):
        act = actuals[h]
        ph = pricing["hours"][h]
        rce = float(ph["rce_pln_kwh"])
        imp = float(ph["import_pln_per_kwh"])
        net = float(act["net_kwh"])
        cf_pairs.append((net, rce, imp))
        perfect_inputs.append(
            HourInputs(
                date=local_date.isoformat(),
                hour=h,
                load_kwh=float(act["load_kwh"]),
                pv_kwh=float(act["pv_kwh"]),
                import_pln_per_kwh=imp,
                export_pln_per_kwh=rce,
                load_source="telemetry",
                pv_source="telemetry",
            )
        )

    actual_total = total_cashflow_pln_for_horizon(cf_pairs)
    planned_total = (
        sum(h.expected_cashflow_pln for h in plan.hours) if plan else None
    )

    perfect_total: float | None = None
    if perfect_inputs:
        soc0 = float(actuals[min(actuals.keys())].get("last_soc_pct") or 50.0)
        perfect_total = optimize_horizon(perfect_inputs, soc_start_pct=soc0).total_cashflow_pln

    uplift_actual = (perfect_total - actual_total) if perfect_total is not None else None
    uplift_planned = (
        (perfect_total - planned_total)
        if perfect_total is not None and planned_total is not None
        else None
    )

    recs = _recommendations(hour_rows, actual_total, perfect_total, planned_total)

    summary = (
        f"Doba {local_date.isoformat()}: cashflow rzeczywisty {actual_total:+.2f} PLN."
    )
    if planned_total is not None:
        summary += f" Plan: {planned_total:+.2f} PLN."
    if perfect_total is not None:
        summary += f" Perfect foresight: {perfect_total:+.2f} PLN (uplift {uplift_actual:+.2f})."

    return DayReview(
        local_date=local_date.isoformat(),
        reviewed_at=datetime.now(UTC).isoformat(),
        plan_id=plan.plan_id if plan else None,
        actual_total_cashflow_pln=actual_total,
        planned_total_cashflow_pln=planned_total,
        perfect_foresight_cashflow_pln=perfect_total,
        uplift_vs_actual_pln=uplift_actual,
        uplift_vs_planned_pln=uplift_planned,
        hours=hour_rows,
        recommendations=recs,
        summary_pl=summary,
    )


def save_review(review: DayReview) -> None:
    ensure_planner_dirs()
    path = PLANNER_REVIEWS_DIR / f"review_{review.local_date}.json"
    path.write_text(
        review.model_dump_json(indent=2),
        encoding="utf-8",
    )
