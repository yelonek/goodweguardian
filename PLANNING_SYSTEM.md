# Plan systemu planowania

Planer **co 10 min** → `state/planner_output.json` (**policy** + parametry). Guardian **co 1 min** → eco-slot, `remaining_kwh` (bez zmian). Moduły: [`docs/planner/`](docs/planner/).

- **Horyzont:** godziny od `now` z **oboma** cenami (RCE + import), do **ostatniej znanej** godziny; braków **nie** uzupełniamy prognozą RCE.
- **Serie:** `pv_plan = k×p50`, `load_plan` = p50 × korekta (brak danych → **1,0**) + sloty.
- **Cel:** **max Σ** cashflow PLN / godz. (jak KPI).
- **Histereza:** nowa policy zapisuje się tylko przy różnicy prognozy cashflow na horyzoncie **≥ 0,5 PLN**.

---

## 12. Konsensus (normatywne)

1. **Architektura:** planer **co 10 min**; Guardian **co minutę**. Histereza zapisu policy: **≥ 0,5 PLN** różnicy cashflow na horyzoncie.

2. **Cel ekonomiczny:** max sumy cashflow PLN; `net_kWh > 0` → **`+ net_kWh × RCE`**; `net_kWh < 0` → **`net_kWh × import_pln_per_kwh`**.

3. **Dane do optimizera:** `pv_plan[h] = k × pv_p50[h]`, `load_plan[h]` jak wyżej. Jedna optymalizacja **max Σ_h cashflow_h**; **rolling** co cykl.

4. **Wyjście:** **policy + parametry**; optimizer → wektor `e_bat_kwh[h]` w granicach **battery_model**; **policy_output** → enum + JSON. Guardian mapuje policy na sterowanie.

5. **Bateria w solverze:** `soc_kwh`, limity z `.env`, **jedno η** round-trip (`η_rt`).

6. **Korekta PV (`k`, `u` tylko w metrykach):**
   - **ε = 0,1 kWh/h** (agregacja godzinowa telemetrii vs prognozy).
   - Godzina znacząca: **(p50 > ε) ∨ (faktyczna w zamkniętej h > ε)**; przeszłość z `/history` gdy brak w snapshocie.
   - Pierwsza znacząca **h** od północy; brak do `now` → **`k = 1`**.
   - **`start = max`(początek pierwszej znaczącej h, `now − 3 h`)**; koniec okna = **`now`**.
   - Sumy w oknie: **wszystkie pełne** godziny od `start` do godziny **poprzedzającej** bieżącą + dla **bieżącej** h: **α ×** energia (prognoza i faktyczna), **α = (minuta + sekunda/60) / 60**.
   - **`k = clip(A / F50, k_min, k_max)`** przy **`k_min = 0,65`**, **`k_max = 1,35`**; **`F50 < 10⁻⁶` kWh** → **`k = 1`**; **`start ≥ now`** → **`k = 1`**.
   - **`u`:** interpolacja **A** względem **F10, F50, F90** w oknie (szczegół w `docs/planner/modules/pv_correction.md`); **nie** w funkcji celu.

7. **Solcast:** `fetched_at`, `age_hours`, `GET /status` — logi; okno **k** nie zależy od harmonogramu fetchy.

8. **Horyzont cen:** tylko godziny z **`rce_pln_kwh`** i **`import_pln_per_kwh`**. Koniec = **ostatnia znana** h z parą (np. do **24:00** dnia dostawy w feedzie). Późniejsze h bez pary — poza planem.

9. **Wdrożenie (kolejność):** kontrakt danych (ceny, telemetria, KPI) → symulator offline (`pv_plan`, `load_plan`, `e_bat` → Σ PLN + testy) → cykl planera co 10 min + zapis JSON → Guardian czyta policy → UI (sloty, sugestie).

---

*Zmiany produktowe = aktualizacja tego pliku (§12) + `docs/planner/`.*
