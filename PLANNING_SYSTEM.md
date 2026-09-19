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

4. **Wyjście:** **policy + parametry**. Planer publikuje jawną decyzję bieżącego kroku (`planned_charge_kwh`, `planned_discharge_kwh`, target SOC/net, zgodę i budżet sieci). Dla przyszłości publikuje oczekiwaną trajektorię i pasma światów; te wiersze zostaną przeliczone przed wykonaniem.

5. **Bateria w solverze:** `soc_kwh`, limity z Ustawień (`planner_soc_*`, `battery_capacity_kwh`), **jedno η** round-trip (`η_rt`). W bilansie SOC: `+√η_rt · ch − dis / √η_rt` (symetrycznie), żeby cykl AC→AC odzyskiwał dokładnie `η_rt`, a nie `η_rt²`.

   **Stochastic MPC:** stan początkowy i decyzja `h=0` są wspólne (non-anticipativity). Dla `h>0` każdy scenariusz ma własne `soc`, `charge`, `discharge`, `import` i `export`, bo planer uruchomi się ponownie przed wykonaniem tych godzin. Cel: `min E(import_cost − export_revenue + wear)`, bez CVaR, kary tracking i wartości końcowej baterii. Stary shared-battery MILP działa tylko jako kontrola shadow.

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
   - **Pasma reszty bieżącej h (p10/p90 PV, p25/p75 load):** zwężane z `(1−α)`, floor od energii so_far; MILP dostaje **pełną godzinę** `so_far+rem`, a `hour_fraction` tylko limit mocy — patrz [`docs/planner/modules/pv_correction.md`](docs/planner/modules/pv_correction.md), [`load_correction.md`](docs/planner/modules/load_correction.md).

7. **Solcast:** `fetched_at`, `age_hours`, `GET /status` — logi.

8. **Horyzont cen:** tylko godziny z **`rce_pln_kwh`** i **`import_pln_per_kwh`**. Koniec = **ostatnia znana** h z parą (np. do **24:00** dnia dostawy w feedzie). Późniejsze h bez pary — poza planem.

9. **Wdrożenie (kolejność):** kontrakt danych (ceny, telemetria, KPI) → symulator offline (`pv_plan`, `load_plan`, `e_bat` → Σ PLN + testy) → cykl planera co 10 min + zapis JSON → Guardian: router policy → strategia (§13) → UI (sloty, sugestie).

10. **Nocny anty-flipflop (twarda polityka, bez pokrętła):** w oknie zegarowym **22–5** (nie strefa G12 — bez 13–14), po pierwszym istotnym ładowaniu magazynu (`charge > 0,05 kWh`, przy nocnym znikomym PV oznacza zakup z sieci) **zakaz eksportu do sieci** aż do końca tego okna. Wieczorny zrzut, potem nocny zakup — dozwolone. Od **6:00** energia kupiona z sieci może zostać sprzedana, jeżeli pełny horyzont wykazuje zysk po η i wear. Carry-in z telemetrii podtrzymuje zapadkę po wcześniejszym nocnym ładowaniu. `green_stock` pozostaje wyłącznie diagnostyką i nie ogranicza solve.

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

### 13.3 Biegi eco-slotu (`exec_mode`)

Prawdziwy wachlarz zachowań GoodWe to **sześć biegów** sterowania (nie mylić z samym znakiem netu na liczniku):

| Bieg | Eco-slot | `exec_mode` |
|------|----------|-------------|
| 1 | **DISCHARGE 2–100%** | `export_profit` |
| 2 | **DISCHARGE 1%** | `export_pv_surplus` |
| 3 | **neutral** (brak stałego %; logika Flappy) | `neutral` |
| 4 | **CHARGE 1%** | `import_grid` |
| 5 | **CHARGE 2–100%, tylko PV** | `charge_pv` |
| 6 | **CHARGE 2–100%, z sieci** | `charge_grid` |

Planer wybiera **`exec_mode`** z jawnych przepływów pełnego solve: `planned_charge_kwh` → ładowanie (`charge_pv` albo `charge_grid`), `planned_discharge_kwh` → rozładowanie, a zerowa zmiana SOC → przepuszczenie PV/import/neutral według net. Nie stosuje progu „tania taryfa” do nadpisywania decyzji ekonomicznej.

| `exec_mode` | PL | Eco-slot | Parametry | Sens |
|-------------|-----|----------|-----------|------|
| `export_profit` | eksport zarobkowy | DISCHARGE **2–100%** | `discharge_pct`, `soc_floor_pct` | Sprzedaż z baterii (wysokie RCE); nie schodzić poniżej podłogi SOC |
| `export_pv_surplus` | eksport nadwyżek PV | **DISCHARGE 1%** lub soak po limicie | `max_additional_export_kwh` | PV → sieć tylko do limitu wybranego przez solve |
| `neutral` | neutralny | Flappy (§13.5) | `target_net_kwh` | Minimalna ingerencja baterii; pilnuj `target` regułami Flappy |
| `import_grid` | import z sieci | **CHARGE 1%**, cel slotu **SOC 10%** (stałe) | — | Dom z sieci; bateria **tylko z PV** (DC); bez ładowania magazynu z sieci |
| `charge_pv` | ładuj z PV | CHARGE **2–100%** | `planned_charge_kwh`, `target_soc_pct` | Soak nadwyżki `PV − load`; **zakaz** importu do magazynu |
| `charge_grid` | ładowanie z sieci | CHARGE **2–100%** | `planned_charge_kwh`, `target_soc_pct`, zgoda/budżet sieci | Najpierw live PV; sieć tylko do budżetu |

**Obrony SOC** — warstwa nadrzędna przed `exec_mode`, z wyjątkami:

| Obrona | Działa w trybach |
|--------|------------------|
| **Pełna bateria** (blokada rozładowania) | wszystkie **oprócz** `export_profit` |
| **Niska bateria** (liniowy sufit mocy DISCHARGE) | clamp na każdej decyzji `mode=discharge` (wszystkie tryby z rozładowaniem); nie osobna strategia trybu |
| **Rezerwa nocna** | zawsze (jak dotychczas) |

Przy `export_profit` i SOC 99% planer **może** rozładowywać — obrona pełnej nie blokuje. Podłogę SOC w tym trybie pilnuje `soc_floor_pct` w strategii, nie `soc_full_defense`.

Enum `hold_*` w kodzie (dziś) **do zastąpienia** przez `exec_mode`.

### 13.4 `target_net_kwh` — jedno pole, znaczenie zależy od trybu

Brak osobnego `anchor_net_kwh` — **Ockham:** jedno pole, różna interpretacja:

| Tryb | Rola `target_net_kwh` |
|------|------------------------|
| `neutral` | **Setpoint Flappy** — bilans do utrzymania; planer ustawia przy wejściu planu (co 10 min), np. `actual` lub skorygowana wartość |
| `export_pv_surplus`, `export_profit` | Limit końcowego eksportu; nadmiar live PV nie zwiększa go automatycznie |
| `import_grid`, `charge_pv`, `charge_grid` | Audyt końca h; charge dodatkowo ogranicza SOC (`charge_grid` też budżet sieci) |

**Mid-hour:** energie PV/load w MILP to **pełna godzina** (so_far + zwężona reszta); `hour_fraction` ogranicza tylko moc baterii (`charge`/`discharge`). `net_so_far` (`N₀`) wchodzi do bilansu tak, że `import`/`export` oznaczają rozliczenie **końca godziny**. `target_net_remainder_kwh = target_net_kwh − N₀`.

„Pilnować” w `neutral` ≠ gonić co minutę — reguły Flappy (§13.5).

### 13.5 Zachowanie per tryb

#### `export_profit` — DISCHARGE 2–100%

- Aktywne rozładowanie baterii do sieci (wysokie RCE); `discharge_pct` z planu lub wyliczone.
- **`soc_floor_pct`:** po osiągnięciu podłogi zmniejszyć discharge do **1%** lub wyjść w neutral w ramach strategii.
- **Obrona niskiego SOC** (`soc_low_cap_*`): między dolnym a górnym SOC strefy sufit mocy maleje liniowo (domyślnie 10%→70 W, 20%→1000 W), bez podbijania do loadu — reszta z sieci/PV. Clamp na każdej decyzji DISCHARGE.

#### `export_pv_surplus` — eksport nadwyżek PV

- **`DISCHARGE 1%`** przepuszcza PV, dopóki bilans nie osiągnie planowanego limitu.
- Po osiągnięciu limitu dodatkowa nadwyżka live PV jest soakowana; już sprzedanych kWh nie odkupujemy.
- Bateria **tylko** przy bilansie godzinowym **&lt; 0** (load zjadł PV); podłoga **0**, nie target planu.

#### `neutral` — Flappy Bird względem `target_net_kwh`

Utrzymuj **`target_net_kwh`** z planu (aktualizacja przy wejściu planu), nie domyślne zero. O `:40` przy `target = +2` → **nie** ładuj na siłę do zera.

1. **Load &gt; PV, bilans ≥ target** → **nic** (pozwól bilansowi spaść), **chyba że** okno końca h i **drobny** ogonek eksportu (≤ 0,5 kWh) przy wolnym SOC → CHARGE soak. Self-consumption z baterii **nie** zjada wyeksportowanych kWh. **Nie** odkupuj całogodzinnego zrzutu (`actual ≫ target`) importem z sieci — to sprzedana energia, nie uwięziony ogonek.
2. **Bilans &lt; target** → najpierw **PV** (1% discharge gdy PV ≥ load).
3. **Bilans &lt; 0, PV nie nadrobi** → korekta deficytu baterią, limit **`min(P_bat, P_inverter − PV_w)`**; **nie** gdy plan `battery_delta &gt; 0` (godzina ładowania — nie zrzucaj magazynu za stale PV).
4. **Bilans &gt; target, PV &gt; load** → ładuj z PV (soak).
5. **Bilans &gt; target, PV ≤ load, poza oknem końca h** → neutral.

#### `import_grid` — CHARGE 1% + SOC 10% (stałe)

- Zawsze **`CHARGE 1%`** i **cel slotu SOC = 10%** — wartość **stała** w Guardianie (nie parametr planera). Mechanizm jak ręczne ustawienie na inwerterze: niski próg slotu **uniemożliwia** ładowanie baterii z sieci.
- **Sieć → dom** (tanio). **Bateria tylko z PV** (DC→DC, „co łaska”).
- **Nie** rozładowuj baterii; import na liczniku jest **zgodny z intencją**.
- Chcesz **ładować magazyn z sieci** → planer wybiera **`charge_grid`**, nie `import_grid`.
- Chcesz **schować PV w magazynie bez importu** → **`charge_pv`**, nie `charge_grid` i nie Flappy.

#### `charge_pv` — CHARGE 2–100% tylko z nadwyżki PV

- Aktywne **`CHARGE`** do `planned_charge_kwh` / **`target_soc_pct`**.
- Cap mocy = live `max(0, PV − load)`. Bez nadwyżki — brak slotu, zero importu „przy okazji”.
- SOC ≥ cel → Off. **Zakaz** `DISCHARGE` i **zakaz** budżetu sieci.

#### `charge_grid` — CHARGE 2–100%

- Aktywne **`CHARGE`** do planowanego `planned_charge_kwh` i **`target_soc_pct`**.
- Guardian najpierw wykorzystuje live PV. `allow_grid_charge` zezwala na brakującą część tylko do `grid_charge_budget_kwh`.
- SOC ≥ cel → zejdź na **1%** lub neutral. **Zakaz** `DISCHARGE`.

### 13.6 Rola parametrów w `planner_output.json`

| Parametr | Planer | Guardian |
|----------|--------|----------|
| `exec_mode` | bieg eco-slotu (§13.3) | router → strategia |
| `target_net_kwh` | `neutral`: setpoint Flappy; inne: audyt / prognoza końca h | patrz §13.4 |
| `soc_floor_pct` | `export_profit` | podłoga SOC przy rozładowaniu |
| `target_soc_pct` | `charge_pv`, `charge_grid` | cel slotu SOC (Y%) |
| `discharge_pct` | `export_profit` | 2–100% |
| `charge_pct` | `charge_pv`, `charge_grid` | 2–100% |
| — | `import_grid` | Guardian: stałe **CHARGE 1%** + **SOC 10%** (poza JSON planera) |
| `planned_charge_kwh`, `planned_discharge_kwh` | wykonalna decyzja z pełnego horyzontu | pacing bieżącego kroku |
| `allow_grid_charge`, `grid_charge_budget_kwh` | ekonomiczna zgoda i maks. uzupełnienie | cap aktywnego charge z sieci |
| `max_additional_export_kwh` | limit eksportu od chwili solve | soak live PV po osiągnięciu limitu |
| `battery_delta_kwh`, `pv_plan_kwh`, `load_plan_kwh` | audyt / kompatybilność | dashboard |
| `soc_end_pct` | oczekiwany SOC (dla h=0 dokładny wspólny wynik) | cel charge; audyt |

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
2. Mapowanie **gap SOC** → `exec_mode` (nie `(net, bd)` jako intencja).
3. Guardian: router; strategie `export_pv_surplus`, `neutral`, `import_grid` (stałe 1% + SOC 10%).
4. `export_profit`, `charge_pv`, `charge_grid`.
5. Wyłączenie `balance_remaining_kwh = actual − target` jako domyślnej egzekucji.

---

*Zmiany produktowe = aktualizacja tego pliku (§12–§13) + `docs/planner/`.*
