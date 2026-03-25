"""
Logika guardiana: stan zbilansowania, próg mocy, histereza, wyznaczanie mocy baterii i czasu.

Konwencja znaku (ujednolicona z mocą sieci):
  − remaining_kWh = ΔE_export − ΔE_import w bieżącej godzinie [kWh].
  − Dodatnie: przewaga energii oddanej do sieci; ujemne: przewaga pobranej z sieci.
  − moc sieci (grid): dodatnie = do sieci (eksport), ujemne = z sieci (import).
Cel: bilans 0 na koniec godziny. Moc baterii: dodatnia = rozładowanie, ujemna = ładowanie.
"""

from dataclasses import dataclass


@dataclass
class BalanceInputs:
    """Wejścia do wyznaczenia interwencji (bez inwertera).

    remaining_kwh: Δeksport − Δimport w tej godzinie [kWh]; + = więcej oddane do sieci.
    """

    remaining_kwh: float
    time_to_end_s: float
    pv_w: float
    consumption_w: float
    grid_w: float
    soc_pct: float
    p_inverter_w: float
    p_battery_w: float
    current_ecoslot_pct: int | None  # None = brak ustawienia w tej godzinie
    slot_active: bool
    hysteresis_start: float
    hysteresis_end: float
    balance_threshold_kw: float = 0.3
    watts_per_percent: float = 70.0
    # True = % z odczytu traktujemy jako „na żywo” (histereza/oscylacja). W runnerze = slot_active
    # (on_off≠0 i czas w oknie); w testach można True przy slot_active=False.
    balancing_slot_time_active: bool = True
    # Inny eco_mode_1..4 (nie balansujący) ma teraz on_off i okno czasu — nie nadpisujemy slotu balansu.
    other_eco_slot_active: bool = False


@dataclass
class BalanceOutput:
    """Wyjście: czy interweniować oraz parametry baterii."""

    intervene: bool
    battery_power_w: float
    battery_power_pct: int
    duration_s: float
    reason: str = ""


def power_needed_kw(remaining_kwh: float, time_to_end_s: float) -> float:
    """Moc potrzebna do zbilansowania [kW] (zawsze >= 0)."""
    if time_to_end_s <= 0:
        return 9999.0
    hours_left = time_to_end_s / 3600.0
    return abs(remaining_kwh) / hours_left


def tolerance_pct(time_to_end_s: float, start_pct: float, end_pct: float) -> float:
    """Tolerancja histerezy [%] – liniowa od start (dużo czasu) do end (mało czasu)."""
    if time_to_end_s >= 3600:
        return start_pct
    if time_to_end_s <= 0:
        return end_pct
    rem_min = time_to_end_s / 60.0
    return end_pct + (start_pct - end_pct) * (rem_min / 60.0)


def _discharge_cap_w(p_inverter_w: float, pv_w: float, p_battery_w: float) -> float:
    """Maks. moc rozładowania [W] – ograniczenie inwertera (PV + BATERIA <= P_INVERTER)."""
    headroom = max(0.0, p_inverter_w - pv_w)
    return min(p_battery_w, headroom)


def _kw_to_pct(power_kw: float, watts_per_percent: float) -> int:
    """Przelicza moc [kW] na % ecoslota (znak zachowany)."""
    power_w = power_kw * 1000.0
    pct = power_w / watts_per_percent
    return max(-100, min(100, int(round(pct))))


def _pct_to_w(pct: int, watts_per_percent: float) -> float:
    """Przelicza % na moc [W] (przybliżenie)."""
    return pct * watts_per_percent


def compute_intervention(inp: BalanceInputs) -> BalanceOutput:
    """
    Wyznacza, czy wykonać interwencję oraz moc baterii [W], [%] i czas ustawienia [s].
    """
    # 1) Slot balansujący aktywny → nie zmieniamy
    if inp.slot_active:
        return BalanceOutput(
            intervene=False,
            battery_power_w=0.0,
            battery_power_pct=0,
            duration_s=0.0,
            reason="slot_active",
        )

    # 1b) Inny ecoslot (1–4) aktywny → nie uruchamiamy zapisu slotu balansującego
    if inp.other_eco_slot_active:
        return BalanceOutput(
            intervene=False,
            battery_power_w=0.0,
            battery_power_pct=0,
            duration_s=0.0,
            reason="other_eco_slot_active",
        )

    # 2) Moc potrzebna do zbilansowania
    power_kw = power_needed_kw(inp.remaining_kwh, inp.time_to_end_s)
    if power_kw <= inp.balance_threshold_kw:
        return BalanceOutput(
            intervene=False,
            battery_power_w=0.0,
            battery_power_pct=0,
            duration_s=0.0,
            reason="power_below_threshold",
        )

    # 3) Kierunek: remaining < 0 (nadmiar importu) → rozładowanie (+P bat); remaining > 0 (nadmiar eksportu) → ładowanie
    sign = -1.0 if inp.remaining_kwh > 0 else 1.0
    target_power_kw = power_kw * sign

    # 4) Cap rozładowanie: nie więcej niż P_INVERTER - PV
    if target_power_kw > 0:
        cap_w = _discharge_cap_w(inp.p_inverter_w, inp.pv_w, inp.p_battery_w)
        target_power_kw = min(target_power_kw, cap_w / 1000.0)
    else:
        target_power_kw = max(target_power_kw, -inp.p_battery_w / 1000.0)

    target_pct = _kw_to_pct(target_power_kw, inp.watts_per_percent)

    live_pct = (
        inp.current_ecoslot_pct if inp.balancing_slot_time_active else None
    )

    # 5) Histereza: porównanie w % (tylko gdy balancing_slot_time_active — inaczej % z rejestru jest tylko konfiguracją)
    tol = tolerance_pct(inp.time_to_end_s, inp.hysteresis_start, inp.hysteresis_end)
    if live_pct is not None:
        if abs(target_pct - live_pct) <= tol:
            return BalanceOutput(
                intervene=False,
                battery_power_w=_pct_to_w(live_pct, inp.watts_per_percent),
                battery_power_pct=live_pct,
                duration_s=0.0,
                reason="hysteresis",
            )
    # 6) Unikanie oscylacji (tylko przy live_pct — bez „żywego” slotu nie blokuj znaku na starym %)
    if live_pct is not None:
        cur = live_pct
        if (cur > 0 and target_pct < 0) or (cur < 0 and target_pct > 0):
            if abs(target_pct) <= tol:
                return BalanceOutput(
                    intervene=False,
                    battery_power_w=_pct_to_w(cur, inp.watts_per_percent),
                    battery_power_pct=cur,
                    duration_s=0.0,
                    reason="oscillation_avoid",
                )

    # 7) Czas ustawienia: nie przestrzelić – duration = energia / moc, cap na time_to_end_s
    if inp.time_to_end_s <= 0:
        duration_s = 0.0
    else:
        energy_kwh = abs(inp.remaining_kwh)
        power_avail_kw = abs(target_power_kw)
        if power_avail_kw <= 0:
            duration_s = 0.0
        else:
            duration_s = min(
                inp.time_to_end_s,
                (energy_kwh / power_avail_kw) * 3600.0,
            )
        duration_s = max(0.0, duration_s)

    battery_w = target_power_kw * 1000.0
    return BalanceOutput(
        intervene=True,
        battery_power_w=battery_w,
        battery_power_pct=target_pct,
        duration_s=duration_s,
        reason="ok",
    )
