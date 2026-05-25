# optimizer

**Cel:** max **Σ** `cashflow_pln(h)` po godzinach **z parą cen** (§12 pkt 8); `net_kwh(h)` z `pv_plan`, `load_plan`, `e_bat[h]` + **battery_model**.

**Wejście:** `PlanSeries`, ceny, SOC start, limity, **η_rt** (bez scipy).

**Wyjście:** `PlannerDecision` — `e_bat_kwh[h]`, `total_cashflow_pln`.

**Solver (jedna metoda):** **spadanie współrzędnych**, siatka **Δ = 0,25 kWh**, start **`e_bat[h]=0`**, max **20** pełnych przejść `h=0…H−1`; przy każdej próbie SOC od zera od telemetrii; w każdej h wybierz wartość z siatki w `[lb,ub]` z **battery_model** co maksymalizuje sumę na całym horyzoncie.
