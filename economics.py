"""Godzinowy cashflow PLN dla planera — zgodny z KPI w ``guardian_dashboard._kpi_for_day``."""

from __future__ import annotations

# Prosument: ujemna RCE nie oznacza dopłaty za eksport — stawka SELL ≥ 0.
EXPORT_PLN_PER_KWH_FLOOR = 0.0


def export_pln_per_kwh_effective(rce_pln_per_kwh: float) -> float:
    """Stawka odkupu energii (SELL) używana w KPI i planerze."""
    return max(EXPORT_PLN_PER_KWH_FLOOR, float(rce_pln_per_kwh))


def cashflow_pln_for_hour(
    net_kwh: float,
    *,
    rce_pln_per_kwh: float,
    import_pln_per_kwh: float,
) -> float:
    """Zysk/strata w jednej godzinie (prosument: eksport = plus, import = minus).

    ``net_kwh > 0`` — nadwyżka eksportu → ``+ net_kwh × max(rce, 0)``.
    ``net_kwh < 0`` — nadwyżka importu → ``net_kwh × import`` (ujemne).
    """
    if net_kwh > 0.0:
        return net_kwh * export_pln_per_kwh_effective(rce_pln_per_kwh)
    if net_kwh < 0.0:
        return net_kwh * import_pln_per_kwh
    return 0.0


def battery_wear_pln_for_hour(
    charge_kwh: float,
    discharge_kwh: float,
    *,
    cycle_cost_pln: float,
) -> float:
    """Amortyzacja: ``cycle_cost_pln`` [PLN/kWh] tylko przy **rozładowaniu** (``charge_kwh`` ignorowane)."""
    if cycle_cost_pln <= 0.0:
        return 0.0
    return cycle_cost_pln * max(0.0, discharge_kwh)


def total_cashflow_pln_for_horizon(
    hours: list[tuple[float, float, float]],
) -> float:
    """Suma cashflow po godzinach.

    Każdy wpis: ``(net_kwh, rce_pln_per_kwh, import_pln_per_kwh)``.
    """
    return sum(
        cashflow_pln_for_hour(
            net,
            rce_pln_per_kwh=rce,
            import_pln_per_kwh=imp,
        )
        for net, rce, imp in hours
    )
