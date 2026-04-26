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
    # Przy niskim SOC: łagodny limit rozładowania liczony z historii zużycia domu [W].
    low_soc_discharge_target_w: float | None = None


@dataclass
class BalanceOutput:
    """Wyjście: czy interweniować oraz parametry baterii."""

    intervene: bool
    battery_power_w: float
    battery_power_pct: int
    duration_s: float
    reason: str = ""


@dataclass
class WatchdogConfig:
    """Polityka watchdog: domyślnie nie steruj, interweniuj późno / awaryjnie."""

    late_window_s: int = 600
    late_power_threshold_kw: float = 0.45
    grid_export_bias_w: float = 150.0
    import_w_threshold: float = -300.0
    import_streak_min: int = 3
    dwell_s: int = 600
    unrecoverable_fraction: float = 0.9
    # remaining<0 i po regule kierunku target_pct==0: ustaw +N% rozładowania (0 = tylko neutral jak dawniej).
    min_discharge_assist_pct: int = 1
    # Charge (redukcja eksportu godzinowego) tylko gdy nadwyżka eksportu > tego progu [kWh] — unikaj sztucznego
    # domykania do zera i wpędzania w import (0 = blokuj charge tylko przy remaining≤0).
    charge_min_remaining_kwh: float = 0.05
    soc_full_threshold_pct: float = 99.5
    soc_full_defense_charge_pct: int = -1
    soc_full_defense_early_release_kwh: float = -0.3
    soc_full_defense_late_release_kwh: float = -0.3
    soc_low_threshold_pct: float = 22.0
    soc_low_defense_charge_pct: int = -1
    # Fallback legacy: trzymaj obronę CHARGE, dopóki remaining_kwh > tego (cel godziny; 0 = zbilansowany).
    soc_low_defense_release_remaining_kwh: float = 0.0
    # Nocna rezerwa SOC: w wybranych godzinach blokuj rozładowanie gdy SOC ≤ progu.
    # 0.0 = wyłączone. Domyślne godziny to ciągły blok nocny 22–5 (przed poranną drogą taryfą).
    soc_night_reserve_pct: float = 0.0
    soc_night_reserve_charge_pct: int = -1
    night_reserve_hours: frozenset[int] = frozenset({22, 23, 0, 1, 2, 3, 4, 5})
    # Pierwsze N minut nowej godziny: kontynuuj tarczę SOC, jeśli była aktywna w ostatnich N min poprzedniej.
    soc_full_defense_carryover_minutes: int = 5
    # Bufor eksportu (sieć): pierwsze N min (0 = wył.) przy PV>konsumpcja, dopóki remaining_kwh < target [kWh].
    export_buffer_build_minutes: int = 15
    export_buffer_target_kwh: float = 0.1
    export_buffer_discharge_pct: int = 1


@dataclass
class WatchdogState:
    """Lekki stan anti-flip-flop + watchdog importu."""

    mode: str = "neutral"  # "charge" | "discharge" | "neutral"
    mode_since_s: float | None = None  # unix seconds
    import_streak: int = 0
    last_remaining_kwh: float | None = None
    # Ustawiane w runnerze: tarcza SOC była w ostatnich minutach poprzedniej godziny (reset bilansu na :00).
    soc_full_defense_carryover: bool = False


@dataclass
class WatchdogDecision:
    """Decyzja watchdog: czy ustawić slot, czy wrócić do neutral."""

    write_slot: bool
    enabled: bool
    power_pct: int
    duration_s: float
    mode: str
    reason: str


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
    # 1) Inny ecoslot (1–4) aktywny → nie uruchamiamy zapisu slotu balansującego
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

    # 3) Zamiast mapować remaining_kwh -> znak baterii, wyznacz docelową moc SIECI.
    # remaining_kwh < 0 (więcej importu) → chcemy eksport (+grid) o wielkości power_kw
    # remaining_kwh > 0 (więcej eksportu) → chcemy import (−grid) o wielkości power_kw
    target_grid_w = (power_kw * 1000.0) * (1.0 if inp.remaining_kwh < 0 else -1.0)

    # Model bilansu chwilowego:
    #   grid_w ≈ pv_w - consumption_w + battery_w
    # stąd:
    #   battery_w_target ≈ target_grid_w - (pv_w - consumption_w)
    target_battery_w = target_grid_w - (inp.pv_w - inp.consumption_w)

    # 4) Ograniczenia baterii oraz inwertera.
    # Bateria: |P| <= P_BATTERY
    target_battery_w = max(-inp.p_battery_w, min(inp.p_battery_w, target_battery_w))
    # Inwerter: PV + rozładowanie <= P_INVERTER (dla discharge)
    if target_battery_w > 0:
        cap_w = _discharge_cap_w(inp.p_inverter_w, inp.pv_w, inp.p_battery_w)
        target_battery_w = min(target_battery_w, cap_w)

    target_pct = max(
        -100, min(100, int(round(target_battery_w / inp.watts_per_percent)))
    )

    live_pct = inp.current_ecoslot_pct if inp.balancing_slot_time_active else None

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
        power_avail_kw = abs(target_battery_w) / 1000.0
        if power_avail_kw <= 0:
            duration_s = 0.0
        else:
            duration_s = min(
                inp.time_to_end_s,
                (energy_kwh / power_avail_kw) * 3600.0,
            )
        duration_s = max(0.0, duration_s)

    return BalanceOutput(
        intervene=True,
        battery_power_w=target_battery_w,
        battery_power_pct=target_pct,
        duration_s=duration_s,
        reason="ok",
    )


def decide_watchdog(
    inp: BalanceInputs,
    *,
    now_s: float,
    state: WatchdogState,
    cfg: WatchdogConfig,
    minute_of_hour: int | None = None,
    hour_of_day: int | None = None,
) -> WatchdogDecision:
    """Watchdog: pozwól GoodWe działać; interweniuj tylko gdy trzeba, potem wróć do neutral."""

    low_soc_discharge_cap_active = (
        float(inp.soc_pct) <= float(cfg.soc_low_threshold_pct)
        and inp.low_soc_discharge_target_w is not None
        and float(inp.low_soc_discharge_target_w) > 0.0
    )

    # Standardowo nie nadpisuj innego eco-slotu. Wyjątek: low-SOC cap chroni LFP przed skokami obciążenia.
    if inp.other_eco_slot_active and not low_soc_discharge_cap_active:
        return WatchdogDecision(
            write_slot=False,
            enabled=False,
            power_pct=0,
            duration_s=0.0,
            mode="neutral",
            reason="other_eco_slot_active",
        )

    # Nocna rezerwa SOC: w godzinach nocnych trzymaj zapas na poranek (przed wschodem słońca).
    if (
        hour_of_day is not None
        and int(hour_of_day) in cfg.night_reserve_hours
        and float(cfg.soc_night_reserve_pct) > 0.0
        and float(inp.soc_pct) <= float(cfg.soc_night_reserve_pct)
    ):
        duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
        return WatchdogDecision(
            write_slot=True,
            enabled=True,
            power_pct=int(cfg.soc_night_reserve_charge_pct),
            duration_s=duration_s,
            mode="charge",
            reason="night_soc_reserve_hold",
        )

    power_kw = power_needed_kw(inp.remaining_kwh, inp.time_to_end_s)

    late = inp.time_to_end_s <= float(cfg.late_window_s)

    carryover_min = max(1, int(cfg.soc_full_defense_carryover_minutes))
    carryover_window_s = float(carryover_min * 60)
    in_soc_full_carryover_window = (
        minute_of_hour is not None
        and int(minute_of_hour) < carryover_min
        and not late
    )

    # SOC=100% defense: utrzymuj CHARGE 1% (blokuj discharge) dopóki bilans nie przekroczy progu.
    # Intencja: przy pełnej baterii tryb CHARGE powinien uniemożliwiać rozładowanie,
    # a PV może i tak trafiać do sieci (brak miejsca w baterii).
    if float(inp.soc_pct) >= float(cfg.soc_full_threshold_pct):
        rel_e = float(cfg.soc_full_defense_early_release_kwh)
        rel_l = float(cfg.soc_full_defense_late_release_kwh)
        # W ostatniej minucie godziny zawsze early: inaczej przy late>early i r≈0 nie ma holdu w :59
        # i falownik zdąży rozładować zanim cykl w :00 zdąży ponownie ustawić slot.
        release_kwh = rel_l if late else rel_e
        if late and float(inp.time_to_end_s) <= 60.0:
            release_kwh = rel_e
        r = float(inp.remaining_kwh)
        early_abs = rel_e
        # Trzymaj defense, dopóki remaining_kwh > release_kwh (early: domyślnie >0 = tylko przy netto-eksporcie).
        if r > release_kwh:
            duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
            return WatchdogDecision(
                write_slot=True,
                enabled=True,
                power_pct=int(cfg.soc_full_defense_charge_pct),
                duration_s=duration_s,
                mode="charge",
                reason="soc_full_defense_hold",
            )
        # Po resecie godziny remaining ~0 — pierwsze N minut: trzymaj obronę dopóki nie ma importu (r < 0),
        # bez wymogu flagi carryover (unikaj „dołka” w :00 gdy Δ godzinowe jeszcze nie urosło).
        if (
            in_soc_full_carryover_window
            and early_abs >= 0.0
            and r >= 0.0
            and r <= release_kwh
        ):
            duration_s = min(inp.time_to_end_s, max(60.0, carryover_window_s))
            return WatchdogDecision(
                write_slot=True,
                enabled=True,
                power_pct=int(cfg.soc_full_defense_charge_pct),
                duration_s=duration_s,
                mode="charge",
                reason="soc_full_defense_carryover",
            )

    # SOC niski: ogranicz rozładowanie do spokojnej mocy bazowej, niezależnie od bilansu godzinowego.
    # Skoki obciążenia (np. czajnik) mają pójść z sieci, a nie wymuszać duży prąd z LFP przy niskim SOC.
    if float(inp.soc_pct) <= float(cfg.soc_low_threshold_pct):
        low_soc_target_w = inp.low_soc_discharge_target_w
        if low_soc_discharge_cap_active:
            load_deficit_w = float(inp.consumption_w) - float(inp.pv_w)
            if load_deficit_w <= 0.0:
                return WatchdogDecision(
                    write_slot=False,
                    enabled=False,
                    power_pct=0,
                    duration_s=0.0,
                    mode="neutral",
                    reason="soc_low_pv_surplus_no_discharge",
                )
            target_w = min(float(low_soc_target_w), load_deficit_w, float(inp.p_battery_w))
            target_pct = max(
                1,
                min(100, int(round(target_w / float(inp.watts_per_percent)))),
            )
            duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
            return WatchdogDecision(
                write_slot=True,
                enabled=True,
                power_pct=target_pct,
                duration_s=duration_s,
                mode="discharge",
                reason="soc_low_discharge_cap",
            )
        # Legacy fallback: jeśli nie ma historii zużycia, zachowaj dawną obronę CHARGE.
        release_kwh = float(cfg.soc_low_defense_release_remaining_kwh)
        if float(inp.remaining_kwh) > release_kwh:
            duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
            return WatchdogDecision(
                write_slot=True,
                enabled=True,
                power_pct=int(cfg.soc_low_defense_charge_pct),
                duration_s=duration_s,
                mode="charge",
                reason="soc_low_defense_hold",
            )

    # Awaryjny watchdog importu (drogi import): reaguj tylko gdy godzina jest już realnie w imporcie netto.
    # Sam chwilowy import z sieci nie może wymuszać charge przy dodatnim bilansie godziny.
    emergency_import = (
        state.import_streak >= cfg.import_streak_min
        and float(inp.remaining_kwh) < 0.0
    )

    # Awaryjny watchdog „unrecoverable”: bilans energii już nie do odrobienia w samym late window
    # E_max_late ≈ P_battery * T_late
    pmax_kw = max(0.0, float(inp.p_battery_w) / 1000.0)
    emax_late_kwh = pmax_kw * (float(cfg.late_window_s) / 3600.0)
    emergency_unrecoverable = abs(inp.remaining_kwh) > (
        emax_late_kwh * float(cfg.unrecoverable_fraction)
    )

    # Wcześnie: nie panikuj — tylko awaryjnie.
    if not late and not emergency_import and not emergency_unrecoverable:
        # Bufor w sieci: tylko przy nadwyżce PV (unikaj „dołka” w nocy), lekki +% aż do docelowej nadwyżki kWh.
        # Nie wchodź tu przy SOC ≥ próg obrony pełnej — soc_full_defense_* wyżej ma pierwszeństwo (bez +% rozładowania).
        if (
            int(cfg.export_buffer_build_minutes) > 0
            and minute_of_hour is not None
            and int(minute_of_hour) < int(cfg.export_buffer_build_minutes)
            and float(inp.pv_w) > float(inp.consumption_w)
            and float(inp.remaining_kwh) < float(cfg.export_buffer_target_kwh)
            and float(inp.soc_pct) > float(cfg.soc_low_threshold_pct)
            and float(inp.soc_pct) < float(cfg.soc_full_threshold_pct)
        ):
            pct = max(1, int(cfg.export_buffer_discharge_pct))
            duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
            return WatchdogDecision(
                write_slot=True,
                enabled=True,
                power_pct=pct,
                duration_s=duration_s,
                mode="discharge",
                reason="export_buffer_build",
            )
        return WatchdogDecision(
            write_slot=False,
            enabled=False,
            power_pct=0,
            duration_s=0.0,
            mode="neutral",
            reason="early_window_no_intervention",
        )

    # W late window: interweniuj dopiero gdy wymagane P jest sensownie duże.
    if (
        late
        and power_kw <= cfg.late_power_threshold_kw
        and not emergency_import
        and not emergency_unrecoverable
    ):
        return WatchdogDecision(
            write_slot=False,
            enabled=False,
            power_pct=0,
            duration_s=0.0,
            mode="neutral",
            reason="late_but_below_threshold",
        )

    # Docelowa moc sieci z biasem na lekki eksport.
    required_w = power_kw * 1000.0
    if inp.remaining_kwh < 0:
        # za dużo importu w energii -> chcemy eksport
        target_grid_w = cfg.grid_export_bias_w + required_w
    else:
        # za dużo eksportu w energii -> chcemy zmniejszyć eksport; unikaj importu jeśli się da
        target_grid_w = max(cfg.grid_export_bias_w, cfg.grid_export_bias_w - required_w)

    target_battery_w = target_grid_w - (inp.pv_w - inp.consumption_w)
    target_battery_w = max(-inp.p_battery_w, min(inp.p_battery_w, target_battery_w))
    if target_battery_w > 0:
        cap_w = _discharge_cap_w(inp.p_inverter_w, inp.pv_w, inp.p_battery_w)
        target_battery_w = min(target_battery_w, cap_w)

    target_pct = max(
        -100, min(100, int(round(target_battery_w / inp.watts_per_percent)))
    )

    # Reguła kierunku + asymetria kosztowa: wolimy lekką nadwyżkę eksportu niż import z sieci.
    # - Nieładuj, dopóki remaining_kwh ≤ charge_min_remaining_kwh (ujemne, zero, mała nadwyżka).
    # - Przy remaining>0 nie dopuszczaj DISCHARGE (już za dużo eksportu w kWh).
    if target_pct < 0 and float(inp.remaining_kwh) <= float(cfg.charge_min_remaining_kwh):
        target_pct = 0
    elif inp.remaining_kwh > 0 and target_pct > 0:
        target_pct = 0

    # Emergency import ma pomagać baterią (discharge), nigdy wymuszać ładowania.
    if emergency_import and target_pct < 0:
        target_pct = 0

    min_discharge_assist = max(0, min(100, int(cfg.min_discharge_assist_pct)))
    used_export_assist = False
    if (
        target_pct == 0
        and inp.remaining_kwh < 0
        and min_discharge_assist > 0
    ):
        # Bilans wymaga eksportu; matematyka dała 0% (np. po zablokowaniu ładowania). Minimalne +%
        # pozwala GoodWe utrzymać sensowny eco-slot i w praktyce „puścić” nadwyżkę PV do sieci.
        target_pct = min_discharge_assist
        used_export_assist = True

    # Anti flip-flop: jeśli jesteśmy świeżo po zmianie trybu, nie zmieniaj znaku (chyba że late lub awaryjnie)
    seconds_in_mode = (
        999999.0
        if state.mode_since_s is None
        else max(0.0, now_s - float(state.mode_since_s))
    )
    desired_mode = "neutral"
    if target_pct > 0:
        desired_mode = "discharge"
    elif target_pct < 0:
        desired_mode = "charge"
    else:
        return WatchdogDecision(
            write_slot=False,
            enabled=False,
            power_pct=0,
            duration_s=0.0,
            mode="neutral",
            reason="direction_guard_neutral",
        )

    if (
        state.mode in ("charge", "discharge")
        and desired_mode in ("charge", "discharge")
        and desired_mode != state.mode
        and seconds_in_mode < float(cfg.dwell_s)
        and not emergency_import
        and not late
    ):
        # Za wcześnie na flip: wróć do neutral i poczekaj (sieć jako bufor)
        return WatchdogDecision(
            write_slot=False,
            enabled=False,
            power_pct=0,
            duration_s=0.0,
            mode="neutral",
            reason="dwell_block_flip",
        )

    # Ustaw krótko; runner i tak ogranicza okno slotu.
    duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
    if emergency_unrecoverable and not late:
        reason = "emergency_unrecoverable"
    elif emergency_import and not late:
        reason = "emergency_import"
    elif emergency_unrecoverable and late:
        reason = "late_unrecoverable"
    elif emergency_import and late:
        reason = "late_emergency_import"
    elif used_export_assist:
        reason = "min_discharge_export_assist"
    else:
        reason = "ok"
    return WatchdogDecision(
        write_slot=True,
        enabled=True,
        power_pct=target_pct,
        duration_s=duration_s,
        mode=desired_mode,
        reason=reason,
    )
