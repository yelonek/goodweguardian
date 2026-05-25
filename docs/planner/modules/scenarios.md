# plan_series

**Wejście:** lista godzin z cenami (§12 pkt 8), `k`, `pv_p50[h]`, `load_p50[h]`, opcjonalnie `factor`, sloty.

**Wyjście:** `PlanSeries` — `pv_plan[h] = k × pv_p50[h]`, `load_plan[h] = load_p50[h] × factor` (+ kWh slotów w wskazanych h).

**Kroki:** (1) PV, (2) load, (3) factor, (4) sloty.
