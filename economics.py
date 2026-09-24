"""Rachunek sprzedawcy za jeden bilans godzinowy.

Dodatni bilans idzie cały po ``max(RCE, 0)``, ujemny cały po G12.
Otwarta godzina dokłada bieżący bilans i resztę. KPI, Planer i Guardian
wołają to miejsce i nie odejmują licznika od planu drugi raz.
"""

from __future__ import annotations

from dataclasses import dataclass

# Prosument: ujemna RCE nie oznacza dopłaty za eksport — stawka SELL ≥ 0.
EXPORT_PLN_PER_KWH_FLOOR = 0.0


def export_pln_per_kwh_effective(rce_pln_per_kwh: float) -> float:
    """Stawka odkupu energii (SELL) używana w KPI i planerze."""
    return max(EXPORT_PLN_PER_KWH_FLOOR, float(rce_pln_per_kwh))


def avg_export_pln_per_kwh(deposit_pln: float, export_kwh: float) -> float | None:
    """Zrealizowana średnia cena sprzedaży 1 kWh: depozyt / nadwyżka netto.

    ``None`` gdy nie było eksportu (``export_kwh <= 0``) — nie dzielimy przez zero.
    """
    if float(export_kwh) <= 0.0:
        return None
    return float(deposit_pln) / float(export_kwh)


@dataclass(frozen=True)
class SellerHour:
    """Rachunek jednej godziny. Reszta jest ustawiona tylko dla otwartej godziny."""

    hourly_balance_kwh: float
    bill_pln: float
    rce_pln_per_kwh: float
    import_pln_per_kwh: float
    current_balance_kwh: float | None = None
    remainder_kwh: float | None = None

    @property
    def prices_at_rce(self) -> bool:
        return self.hourly_balance_kwh > 0.0

    @property
    def prices_at_g12(self) -> bool:
        return self.hourly_balance_kwh < 0.0

    @property
    def unsold_rce_pln_per_kwh(self) -> float | None:
        """Utracone korzyści za kWh ściągnięte z dodatniego bilansu. Nie G12."""
        if not self.prices_at_rce:
            return None
        return self.rce_pln_per_kwh


def settle_seller_hour(
    hourly_balance_kwh: float,
    *,
    rce_pln_per_kwh: float,
    import_pln_per_kwh: float,
    current_balance_kwh: float | None = None,
) -> SellerHour:
    """Wycena bilansu godzinowego. ``current_balance_kwh`` tylko w otwartej godzinie.

    Rachunek idzie od bilansu godzinowego, nie od bieżącego bilansu.
    Reszta = bilans godzinowy − bieżący bilans.
    """
    balance = float(hourly_balance_kwh)
    rce = export_pln_per_kwh_effective(rce_pln_per_kwh)
    imp = float(import_pln_per_kwh)
    remainder = None
    current = None
    if current_balance_kwh is not None:
        current = float(current_balance_kwh)
        remainder = balance - current
    if balance > 0.0:
        bill = balance * rce
    elif balance < 0.0:
        bill = balance * imp
    else:
        bill = 0.0
    return SellerHour(
        hourly_balance_kwh=balance,
        bill_pln=bill,
        rce_pln_per_kwh=rce,
        import_pln_per_kwh=imp,
        current_balance_kwh=current,
        remainder_kwh=remainder,
    )


def cashflow_pln_for_hour(
    net_kwh: float,
    *,
    rce_pln_per_kwh: float,
    import_pln_per_kwh: float,
) -> float:
    """Rachunek zamkniętej godziny: ``settle_seller_hour`` bez bieżącego bilansu."""
    return settle_seller_hour(
        net_kwh,
        rce_pln_per_kwh=rce_pln_per_kwh,
        import_pln_per_kwh=import_pln_per_kwh,
    ).bill_pln


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
