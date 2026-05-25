"""Godzinowa rekonsyliacja: plan vs telemetria vs ceny."""

from __future__ import annotations

from datetime import date

from economics import cashflow_pln_for_hour
from energy_pricing import pricing_day_breakdown
from planner.models import DailyPlan, HourInputs, HourReconciliation
from planner.optimizer import optimize_horizon
from planner.plan_store import hour_plan_from, plan_effective_at
from planner.telemetry import hourly_actuals


def reconcile_hour(
    *,
    local_date: date,
    hour: int,
    plan: DailyPlan | None = None,
    actuals: dict[int, dict],
    pricing: dict,
) -> HourReconciliation:
    if plan is None:
        plan = plan_effective_at(local_date, hour)
    ph = pricing["hours"][hour]
    rce = float(ph["rce_pln_kwh"])
    imp = float(ph["import_pln_per_kwh"])

    planned_net: float | None = None
    planned_cf: float | None = None
    forecast_load: float | None = None
    forecast_pv: float | None = None

    hp = hour_plan_from(plan, local_date, hour)
    if hp is not None:
        planned_net = hp.target_net_kwh
        planned_cf = hp.expected_cashflow_pln
    if plan:
        snap = plan.inputs_snapshot
        for row in snap.get("load_forecast", {}).get("hours", []):
            if int(row.get("hour", -1)) == hour and row.get("date") == local_date.isoformat():
                forecast_load = float(row.get("load_kwh_p50", 0.0))
                break

    act = actuals.get(hour)
    actual_net = float(act["net_kwh"]) if act else None
    actual_load = float(act["load_kwh"]) if act else None
    actual_pv = float(act["pv_kwh"]) if act else None

    actual_cf = None
    if actual_net is not None:
        actual_cf = cashflow_pln_for_hour(
            actual_net, rce_pln_per_kwh=rce, import_pln_per_kwh=imp
        )

    counterfactual_pln: float | None = None
    gap: float | None = None
    notes: list[str] = []

    if act and actual_cf is not None:
        hin = HourInputs(
            date=local_date.isoformat(),
            hour=hour,
            load_kwh=actual_load or 0.0,
            pv_kwh=actual_pv or 0.0,
            import_pln_per_kwh=imp,
            export_pln_per_kwh=rce,
            load_source="telemetry_actual",
            pv_source="telemetry_actual",
        )
        soc = float(act.get("last_soc_pct") or 50.0)
        try:
            opt = optimize_horizon([hin], soc_start_pct=soc)
            counterfactual_pln = opt.total_cashflow_pln
            gap = counterfactual_pln - actual_cf
        except Exception:
            counterfactual_pln = None
            gap = None
        if gap > 0.05:
            notes.append(
                f"Przy znanych PV/load można było zyskać ~{gap:.2f} PLN więcej w tej godzinie."
            )

    if planned_net is not None and actual_net is not None:
        dev = actual_net - planned_net
        if abs(dev) > 0.15:
            notes.append(f"Odchylenie od planu net: {dev:+.2f} kWh.")

    if forecast_load is not None and actual_load is not None:
        err = actual_load - forecast_load
        if abs(err) > 0.3:
            notes.append(f"Błąd prognozy load: {err:+.2f} kWh.")

    return HourReconciliation(
        date=local_date.isoformat(),
        hour=hour,
        plan_id_at_hour=plan.plan_id if plan else None,
        plan_generated_at=plan.generated_at if plan else None,
        planned_net_kwh=planned_net,
        actual_net_kwh=actual_net,
        planned_cashflow_pln=planned_cf,
        actual_cashflow_pln=actual_cf,
        forecast_load_kwh=forecast_load,
        actual_load_kwh=actual_load,
        forecast_pv_kwh=forecast_pv,
        actual_pv_kwh=actual_pv,
        counterfactual_optimal_pln=counterfactual_pln,
        gap_vs_optimal_pln=gap,
        notes=notes,
    )
