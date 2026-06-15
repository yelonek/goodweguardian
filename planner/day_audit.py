"""Dzienny audyt: rzeczywisty cashflow vs perfect foresight (znane PV/load z telemetrii)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from economics import cashflow_pln_for_hour, total_cashflow_pln_for_horizon
from energy_pricing import pricing_day_breakdown
from planner.config import PLANNER_AUDITS_DIR, ensure_planner_dirs
from planner.models import DayAudit, HourAuditRow, HourInputs
from planner.optimizer import optimize_horizon
from planner.telemetry import hourly_actuals


def build_day_audit(local_date: date) -> DayAudit:
    actuals = hourly_actuals(local_date)
    pricing = pricing_day_breakdown(local_date)

    hour_rows: list[HourAuditRow] = []
    cf_pairs: list[tuple[float, float, float]] = []
    perfect_inputs: list[HourInputs] = []

    for h in sorted(actuals.keys()):
        act = actuals[h]
        ph = pricing["hours"][h]
        rce = float(ph["rce_pln_kwh"])
        imp = float(ph["import_pln_per_kwh"])
        net = float(act["net_kwh"])
        load_kwh = float(act["load_kwh"])
        pv_kwh = float(act["pv_kwh"])
        actual_cf = cashflow_pln_for_hour(
            net, rce_pln_per_kwh=rce, import_pln_per_kwh=imp
        )
        cf_pairs.append((net, rce, imp))
        perfect_inputs.append(
            HourInputs(
                date=local_date.isoformat(),
                hour=h,
                load_kwh=load_kwh,
                pv_kwh=pv_kwh,
                import_pln_per_kwh=imp,
                export_pln_per_kwh=rce,
                load_source="telemetry",
                pv_source="telemetry",
            )
        )
        hour_rows.append(
            HourAuditRow(
                hour=h,
                actual_net_kwh=net,
                actual_cashflow_pln=actual_cf,
                actual_load_kwh=load_kwh,
                actual_pv_kwh=pv_kwh,
            )
        )

    actual_total = total_cashflow_pln_for_horizon(cf_pairs)

    perfect_total: float | None = None
    optimal_by_hour: dict[int, float] = {}
    if perfect_inputs:
        soc0 = float(actuals[min(actuals.keys())].get("last_soc_pct") or 50.0)
        opt = optimize_horizon(perfect_inputs, soc_start_pct=soc0)
        perfect_total = opt.total_cashflow_pln
        for hp in opt.hours:
            optimal_by_hour[hp.hour] = hp.target_net_kwh

    uplift: float | None = None
    if perfect_total is not None:
        uplift = perfect_total - actual_total

    enriched_rows: list[HourAuditRow] = []
    for row in hour_rows:
        opt_net = optimal_by_hour.get(row.hour)
        opt_cf = None
        gap = None
        if opt_net is not None:
            ph = pricing["hours"][row.hour]
            opt_cf = cashflow_pln_for_hour(
                opt_net,
                rce_pln_per_kwh=float(ph["rce_pln_kwh"]),
                import_pln_per_kwh=float(ph["import_pln_per_kwh"]),
            )
            gap = opt_cf - row.actual_cashflow_pln
        enriched_rows.append(
            row.model_copy(
                update={
                    "optimal_net_kwh": opt_net,
                    "optimal_cashflow_pln": opt_cf,
                    "gap_vs_optimal_pln": gap,
                }
            )
        )

    summary = (
        f"Doba {local_date.isoformat()}: rzeczywisty cashflow {actual_total:+.2f} PLN."
    )
    if perfect_total is not None and uplift is not None:
        summary += (
            f" Perfect foresight: {perfect_total:+.2f} PLN "
            f"(uplift {uplift:+.2f} PLN vs fakty)."
        )

    return DayAudit(
        local_date=local_date.isoformat(),
        audited_at=datetime.now(UTC).isoformat(),
        actual_total_cashflow_pln=actual_total,
        perfect_foresight_cashflow_pln=perfect_total,
        uplift_vs_actual_pln=uplift,
        hours=enriched_rows,
        summary_pl=summary,
    )


def _audit_path(local_date: date) -> Path:
    return PLANNER_AUDITS_DIR / f"audit_{local_date.isoformat()}.json"


def load_day_audit(local_date: date) -> DayAudit | None:
    """Wczytaj zapisany snapshot audytu lub ``None`` gdy brak pliku / błąd parsowania."""
    path = _audit_path(local_date)
    if not path.exists():
        return None
    try:
        return DayAudit.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def get_day_audit(
    local_date: date,
    *,
    recompute_if_missing: bool = True,
    save_if_recomputed: bool = False,
) -> tuple[DayAudit | None, str]:
    """
    Saved-first odczyt audytu.

    Returns:
        (audit, source) gdzie ``source`` ∈ ``saved`` | ``recomputed`` | ``missing``.
    """
    saved = load_day_audit(local_date)
    if saved is not None:
        return saved, "saved"
    if not recompute_if_missing:
        return None, "missing"
    actuals = hourly_actuals(local_date)
    if not actuals:
        return None, "missing"
    audit = build_day_audit(local_date)
    if save_if_recomputed:
        save_day_audit(audit)
    return audit, "recomputed"


def save_day_audit(audit: DayAudit) -> None:
    ensure_planner_dirs()
    path = _audit_path(date.fromisoformat(audit.local_date))
    path.write_text(audit.model_dump_json(indent=2), encoding="utf-8")
