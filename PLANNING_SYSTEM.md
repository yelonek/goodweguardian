# Plan systemu planowania

Planer **co 10 min** → `state/planner_output.json` (**policy** + parametry). Guardian **co 1 min** → eco-slot, `remaining_kwh` (bez zmian). Moduły: [`docs/planner/`](docs/planner/).

- **Horyzont:** godziny od `now` z **oboma** cenami (RCE + import), do **ostatniej znanej** godziny; braków **nie** uzupełniamy prognozą RCE.
- **Serie:** `pv_plan` = korekta `k_intra` na **h** i **h+1**, dalej Solcast p50; `load_plan` = p50 × nowcast (brak danych → bez korekty) + sloty.
- **Cel:** **max Σ** cashflow PLN / godz. (jak KPI).

---

## 12. Konsensus (normatywne)

1. **Architektura:** planer **co 10 min**; Guardian **co minutę**.

2. **Cel ekonomiczny:** max sumy cashflow PLN; `net_kWh > 0` → **`+ net_kWh × RCE`**; `net_kWh < 0` → **`net_kWh × import_pln_per_kwh`**.

3. **Dane do optimizera:** `pv_plan[h]` jak w pkt 6; `load_plan[h]` jak wyżej. Jedna optymalizacja **max Σ_h cashflow_h**; **rolling** co cykl.

4. **Wyjście:** **policy + parametry**; optimizer → wektor `e_bat_kwh[h]` w granicach **battery_model**; **policy_output** → enum + JSON. Guardian mapuje policy na sterowanie.

5. **Bateria w solverze:** `soc_kwh`, limity z `.env`, **jedno η** round-trip (`η_rt`).

6. **Korekta PV (`k_intra`):**
   - **ε = 0,1 kWh/h** — próg znaczącej prognozy w ułamku godziny.
   - **α = (minuta + sekunda/60) / 60** — ułamek **bieżącej** godziny lokalnej (od `:00` tej godziny, nie od północy).
   - **A_so_far** — energia PV z telemetrii od początku bieżącej godziny [kWh].
   - **F_elapsed = α × F50_current** — prognoza p50 na minioną część bieżącej godziny.
   - Gdy **F_elapsed > ε × α**: **`k_intra = clip(A_so_far / F_elapsed, k_min, k_max)`** przy **`k_min = 0,65`**, **`k_max = 1,35`**.
   - Gdy warunek nie spełniony (noc, początek godziny, brak telemetrii): **`k_intra` nieaktywne** — surowy Solcast p50.
   - **pv_plan[bieżąca h]** = **`A_so_far + (1−α) × F50 × k_intra`** (gdy `k_intra` aktywne).
   - **pv_plan[h+1]** = **`k_intra × F50`** (gdy `k_intra` aktywne).
   - **pv_plan[h+2…]** = **F50** (Solcast bez korekty).
   - Korekta **nie** obejmuje całego dnia jednym współczynnikiem — przesuwa się co godzinę.

7. **Solcast:** `fetched_at`, `age_hours`, `GET /status` — logi.

8. **Horyzont cen:** tylko godziny z **`rce_pln_kwh`** i **`import_pln_per_kwh`**. Koniec = **ostatnia znana** h z parą (np. do **24:00** dnia dostawy w feedzie). Późniejsze h bez pary — poza planem.

9. **Wdrożenie (kolejność):** kontrakt danych (ceny, telemetria, KPI) → symulator offline (`pv_plan`, `load_plan`, `e_bat` → Σ PLN + testy) → cykl planera co 10 min + zapis JSON → Guardian czyta policy → UI (sloty, sugestie).

---

*Zmiany produktowe = aktualizacja tego pliku (§12) + `docs/planner/`.*
