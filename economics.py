"""Godzinowy cashflow PLN dla planera — zgodny z KPI w ``guardian_dashboard._kpi_for_day``."""

from __future__ import annotations


def cashflow_pln_for_hour(
    net_kwh: float,
    *,
    rce_pln_per_kwh: float,
    import_pln_per_kwh: float,
) -> float:
    """Zysk/strata w jednej godzinie (prosument: eksport = plus, import = minus).

    ``net_kwh > 0`` — nadwyżka eksportu → ``+ net_kwh * rce``.
    ``net_kwh < 0`` — nadwyżka importu → ``net_kwh * import`` (ujemne).
    """
    if net_kwh > 0.0:
        return net_kwh * rce_pln_per_kwh
    if net_kwh < 0.0:
        return net_kwh * import_pln_per_kwh
    return 0.0


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
