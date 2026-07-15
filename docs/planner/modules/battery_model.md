# battery_model

**Krok:** `e_bat_kwh` (znak: + ładuj / − oddawaj, strona AC) + **η_rt** → następny **SOC [kWh]**; clip do **[soc_min, soc_max]** i **|e_bat| ≤ P_BATTERY×1h**; przy rozładunku cap z **P_INVERTER** + `pv_plan[h]` (jedna formuła w kodzie).

**η:** ustawienie `planner_battery_eta` = sprawność **round-trip** (`η_rt`). W kroku SOC i w MILP stosuje się **η₁ = √η_rt** po obu stronach:

- ładowanie: `ΔSOC = E_ch_ac · η₁`
- rozładowanie: `E_dis_ac = ΔSOC · η₁` ⇔ `ΔSOC = E_dis_ac / η₁`

Dzięki temu cykl AC→AC odzyskuje **η_rt**, a nie η_rt².

**Wejście:** SOC, `usable_kwh`, `P_BATTERY`, `P_INVERTER`, `eta_rt`, `e_bat`, `pv_plan[h]` jeśli potrzebne.

**Wyjście:** `soc_next`; dla całego h — trajektoria SOC.
