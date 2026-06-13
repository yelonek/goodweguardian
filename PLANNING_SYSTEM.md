# Plan systemu planowania

Planer **co 10 min** → `state/planner_output.json` (**policy** + parametry). Guardian **co 1 min** → eco-slot według **trybu zachowania** z policy (§13). Moduły: [`docs/planner/`](docs/planner/).

- **Horyzont:** godziny od `now` z **oboma** cenami (RCE + import), do **ostatniej znanej** godziny; braków **nie** uzupełniamy prognozą RCE.
- **Serie:** `pv_plan` = korekta `k_intra` na **h** i **h+1**, dalej Solcast p50; `load_plan` = p50 × nowcast (brak danych → bez korekty) + sloty.
- **Cel:** **max Σ** cashflow PLN / godz. (jak KPI).

---

## 12. Konsensus (normatywne)

1. **Architektura:** planer **co 10 min**; Guardian **co minutę**.

2. **Cel ekonomiczny:** max sumy cashflow PLN; `net_kWh > 0` → **`+ net_kWh × max(RCE, 0)`**; `net_kWh < 0` → **`net_kWh × import_pln_per_kwh`**.

3. **Dane do optimizera:** `pv_plan[h]` jak w pkt 6; `load_plan[h]` jak wyżej. Jedna optymalizacja **max Σ_h cashflow_h**; **rolling** co cykl.

4. **Wyjście:** **policy + parametry**; optimizer → wektor `e_bat_kwh[h]` w granicach **battery_model**; **policy_output** → enum + JSON. Guardian **nie** goni `target_net_kwh` co minutę — wykonuje **strategię** przypisaną do policy (§13).

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

9. **Wdrożenie (kolejność):** kontrakt danych (ceny, telemetria, KPI) → symulator offline (`pv_plan`, `load_plan`, `e_bat` → Σ PLN + testy) → cykl planera co 10 min + zapis JSON → Guardian: router policy → strategia (§13) → UI (sloty, sugestie).

---

## 13. Egzekucja policy w Guardianie (normatywne)

### 13.1 Rozdzielenie odpowiedzialności

| Warstwa | Częstotliwość | Odpowiedzialność |
|---------|---------------|------------------|
| **Planer** | co 10 min | Ekonomia (max Σ cashflow), SOC na horyzoncie, wybór **policy** + parametrów |
| **Guardian** | co 1 min | **Wykonanie trybu** — eco-slot zgodnie ze strategią policy, nie z optymalizatorem co minutę |
| **Obrony SOC** | co 1 min | Bezpieczeństwo baterii — **zawsze** ponad policy (jak dziś) |

**`target_net_kwh`** z planu to wynik optymalizatora i metryka audytu (plan vs fakty, dashboard). **Nie** jest domyślnym setpointem pętli sterowania Guardiana.

Ślepe podążanie za liczbą (`actual_net − target_net` → agresywna korekta) **nie** jest modelem docelowym: prowadzi do niepotrzebnych cykli baterii (gonienie eksportu na początku godziny, dobijanie 0,2 kWh na końcu).

### 13.2 Architektura wykonania

1. Guardian czyta wiersz policy dla bieżącej godziny z `state/planner_output.json` (`policy`, `params`, `valid_until`).
2. **Router** wybiera **strategię** (jedna funkcja / moduł na policy).
3. Strategia zwraca decyzję eco-slot (jak dziś `WatchdogDecision`: %, czas, tryb, reason).
4. Wspólne dla wszystkich strategii: obrony SOC, `other_eco_slot_active`, limit inwertera (`P_INVERTER`), guard kierunku w ramach danej strategii.

Brak pliku policy, `valid_until` w przeszłości lub `degraded` bez wiersza na bieżącą h → **fallback**: zachowanie jak dziś bez planera (Flappy Bird, bilans ~0 na liczniku).

Przełącznik egzekucji planu (dashboard / override) wyłączony → ten sam fallback.

### 13.3 Pięć biegów eco-slotu (`exec_mode`)

Prawdziwy wachlarz zachowań GoodWe to **pięć biegów** sterowania (nie mylić z samym znakiem netu na liczniku):

| Bieg | Eco-slot | `exec_mode` |
|------|----------|-------------|
| 1 | **DISCHARGE 2–100%** | `export_profit` |
| 2 | **DISCHARGE 1%** | `export_pv_surplus` |
| 3 | **neutral** (brak stałego %; logika Flappy) | `neutral` |
| 4 | **CHARGE 1%** | `import_grid` |
| 5 | **CHARGE 2–100%** | `charge_grid` |

Planer wybiera **`exec_mode`** + parametry. Guardian utrzymia odpowiedni bieg; **nie** goni `target_net_kwh` agresywnym chase we wszystkich trybach (§13.4).

| `exec_mode` | PL | Eco-slot | Parametry | Sens |
|-------------|-----|----------|-----------|------|
| `export_profit` | eksport zarobkowy | DISCHARGE **2–100%** | `discharge_pct`, `soc_floor_pct` | Sprzedaż z baterii (wysokie RCE); nie schodzić poniżej podłogi SOC |
| `export_pv_surplus` | eksport nadwyżek PV | **DISCHARGE 1%** | podłoga bilansu **0** | PV → sieć; bateria tylko przy bilansie &lt; 0 |
| `neutral` | neutralny | Flappy (§13.5) | `target_net_kwh` | Minimalna ingerencja baterii; pilnuj `target` regułami Flappy |
| `import_grid` | import z sieci | **CHARGE 1%**, cel slotu **SOC 10%** (stałe) | — | Dom z sieci; bateria **tylko z PV** (DC); bez ładowania magazynu z sieci |
| `charge_grid` | ładowanie z sieci | CHARGE **2–100%** | `charge_pct`, `target_soc_pct` | Doładowanie magazynu **z sieci** (+ PV); cel SOC z planu |

**Obrony SOC** — warstwa nadrzędna przed `exec_mode`, z wyjątkami:

| Obrona | Działa w trybach |
|--------|------------------|
| **Pełna bateria** (blokada rozładowania) | wszystkie **oprócz** `export_profit` |
| **Niska bateria** | tylko `export_pv_surplus`, `export_profit`, `neutral` (nie w `import_grid` / `charge_grid` — slot już CHARGE) |
| **Rezerwa nocna** | zawsze (jak dotychczas) |

Przy `export_profit` i SOC 99% planer **może** rozładowywać — obrona pełnej nie blokuje. Podłogę SOC w tym trybie pilnuje `soc_floor_pct` w strategii, nie `soc_full_defense`.

Enum `hold_*` w kodzie (dziś) **do zastąpienia** przez `exec_mode`.

### 13.4 `target_net_kwh` — jedno pole, znaczenie zależy od trybu

Brak osobnego `anchor_net_kwh` — **Ockham:** jedno pole, różna interpretacja:

| Tryb | Rola `target_net_kwh` |
|------|------------------------|
| `neutral` | **Setpoint Flappy** — bilans do utrzymania; planer ustawia przy wejściu planu (co 10 min), np. `actual` lub skorygowana wartość |
| `export_pv_surplus`, `import_grid` | Prognoza końca h + audyt; egzekucja **nie** chase po tym polu |
| `export_profit`, `charge_grid` | Audyt; granice = SOC (`soc_floor_pct` / `target_soc_pct`) |

„Pilnować” w `neutral` ≠ gonić co minutę — reguły Flappy (§13.5).

### 13.5 Zachowanie per tryb

#### `export_profit` — DISCHARGE 2–100%

- Aktywne rozładowanie baterii do sieci (wysokie RCE); `discharge_pct` z planu lub wyliczone.
- **`soc_floor_pct`:** po osiągnięciu podłogi zmniejszyć discharge do **1%** lub wyjść w neutral w ramach strategii.

#### `export_pv_surplus` — eksport nadwyżek PV

- Cała godzina **`DISCHARGE 1%`** — PV do sieci, **nie** ładuj baterii z nadwyżki (brak soak w tym trybie).
- Oczekiwany dodatni bilans na koniec h; **nie** gonimy planowanego `target_net`.
- Bateria **tylko** przy bilansie godzinowym **&lt; 0** (load zjadł PV); podłoga **0**, nie target planu.

#### `neutral` — Flappy Bird względem `target_net_kwh`

Utrzymuj **`target_net_kwh`** z planu (aktualizacja przy wejściu planu), nie domyślne zero. O `:40` przy `target = +2` → **nie** ładuj na siłę do zera.

1. **Load &gt; PV, bilans ≥ target** → **nic** (pozwól bilansowi spaść).
2. **Bilans &lt; target** → najpierw **PV** (1% discharge gdy PV ≥ load).
3. **Bilans &lt; target, PV nie nadrobi** → bateria, limit **`min(P_bat, P_inverter − PV_w)`**; wczesna interwencja (~1 kW).
4. **Bilans &gt; target, PV &gt; load** → ładuj z PV (soak).
5. **Bilans &gt; target, PV ≤ load** → neutral.

#### `import_grid` — CHARGE 1% + SOC 10% (stałe)

- Zawsze **`CHARGE 1%`** i **cel slotu SOC = 10%** — wartość **stała** w Guardianie (nie parametr planera). Mechanizm jak ręczne ustawienie na inwerterze: niski próg slotu **uniemożliwia** ładowanie baterii z sieci.
- **Sieć → dom** (tanio). **Bateria tylko z PV** (DC→DC, „co łaska”).
- **Nie** rozładowuj baterii; import na liczniku jest **zgodny z intencją**.
- Chcesz **ładować magazyn z sieci** → planer wybiera **`charge_grid`**, nie `import_grid`.

#### `charge_grid` — CHARGE 2–100%

- Aktywne **`CHARGE`** (`charge_pct`) do **`target_soc_pct`** z planu (cel slotu SOC — tu planer ustawia Y%).
- Import **do baterii** dozwolony (`allow_grid_charge`); PV jako dodatek.
- SOC ≥ cel → zejdź na **1%** lub neutral. **Zakaz** `DISCHARGE`.

### 13.6 Rola parametrów w `planner_output.json`

| Parametr | Planer | Guardian |
|----------|--------|----------|
| `exec_mode` | bieg eco-slotu (§13.3) | router → strategia |
| `target_net_kwh` | `neutral`: setpoint Flappy; inne: audyt / prognoza końca h | patrz §13.4 |
| `soc_floor_pct` | `export_profit` | podłoga SOC przy rozładowaniu |
| `target_soc_pct` | tylko `charge_grid` | cel slotu SOC (Y%) |
| `discharge_pct` | `export_profit` | 2–100% |
| `charge_pct` | `charge_grid` | 2–100% |
| — | `import_grid` | Guardian: stałe **CHARGE 1%** + **SOC 10%** (poza JSON planera) |
| `allow_grid_charge` | `charge_grid` | import do baterii; w `import_grid` **nie dotyczy** (brak ładowania z sieci) |
| `battery_delta_kwh`, `pv_plan_kwh`, `load_plan_kwh` | optimizer | dashboard |

### 13.7 Czego świadomie nie robimy

- **Deadband** ani **rampa** `target × (elapsed/hour)` jako główna logika.
- **Jedna pętla** `remaining = actual − target_net` dla wszystkich trybów.
- **Ekonomia w watchdogu** — ceny tylko w planerze (wybór `export_profit` itd.).
- **Mylenie** `import_grid` (CHARGE 1%) z `export_pv_surplus` (DISCHARGE 1%) — **przeciwne** biegi eco-slotu.

### 13.8 Telemetria i audyt

- `exec_mode`, `plan_id`, `target_net_kwh`, `target_soc_pct`,
- `actual_net_kwh`, `reason` strategii.

Po godzinie: reconcile `target_net_kwh` vs fakty; w trakcie h liczy się zgodność **trybu**, nie minutowe trafienie w liczbę.

### 13.9 Wdrożenie

1. Kontrakt JSON: `exec_mode` + `target_net_kwh` / `soc_floor_pct` / `target_soc_pct` (`charge_grid`) / `charge_pct` / `discharge_pct`; `import_grid` bez parametrów SOC w JSON.
2. Mapowanie optimizer → `exec_mode` (zamiast `hold_*`).
3. Guardian: router; strategie `export_pv_surplus`, `neutral`, `import_grid` (stałe 1% + SOC 10%).
4. `export_profit`, `charge_grid`.
5. Wyłączenie `balance_remaining_kwh = actual − target` jako domyślnej egzekucji.

---

*Zmiany produktowe = aktualizacja tego pliku (§12–§13) + `docs/planner/`.*
