# planner_service

**Cykl (co 10 min lub test `now`):**

1. Ceny → lista `h` z (RCE, import) do ostatniej znanej (§12 pkt 8); pusto → `degraded`, minimalny JSON, koniec.
2. PV + load na te `h` + telemetria.
3. `pv_correction` → `k_intra`; `pv_plan` na h, h+1 (reszta Solcast p50).
4. `load_plan` (p50 + nowcast).
5. `optimizer` → `e_bat`, suma PLN.
6. `policy_output` → struktura.
7. Atomowy zapis `state/planner_output.json`.
