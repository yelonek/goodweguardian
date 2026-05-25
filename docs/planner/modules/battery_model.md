# battery_model

**Krok:** `e_bat_kwh` (znak wg projektu) + **η_rt** → następny **SOC [kWh]**; clip do **[soc_min, soc_max]** i **|e_bat| ≤ P_BATTERY×1h**; przy rozładunku cap z **P_INVERTER** + `pv_plan[h]` (jedna formuła w kodzie).

**Wejście:** SOC, `usable_kwh`, `P_BATTERY`, `P_INVERTER`, `eta_rt`, `e_bat`, `pv_plan[h]` jeśli potrzebne.

**Wyjście:** `soc_next`; dla całego h — trajektoria SOC.
