# Pomysły na przyszłość

Notatnik roboczy — **nie norma produktowa**. Szczegóły wdrożenia dopiero po przemyśleniu i testach.

---

## Guardian: usunąć rezerwę nocną SOC

**Kiedy:** dopiero gdy planer będzie **bardziej ostrożny** (lepsze prognozy + konserwatywniejsze decyzje). Na dziś zostaje.

**Co potem usunąć / uprościć:**
- blok `night_soc_reserve` w `guardian_logic.decide_soc_defenses`
- `SOC_NIGHT_RESERVE_*` (`.env`, dashboard override)
- testy `test_night_soc_reserve_*`, wpis w `PLANNING_SYSTEM.md` §13

**Uzasadnienie:** rezerwa nocna to airbag na ślepe zaufanie planowi; gdy planer sam dba o SOC w nocy, warstwa w Guardienie jest zbędna.

---

## Planer: mniej ślepego zaufania prognozom PV i LOAD

**Problem obserwowany:** planer traktuje prognozy PV i LOAD jak pewne — optimizer i `exec_mode` na tym bazują.

**Przykład (2026-06):** wysoki planowany LOAD w danym dniu, mimo że faktyczny szczyt wynikał z **ładowania samochodu w zeszłą sobotę** — lookback load forecast („mediana tej samej godziny z ostatnich N dni”) **nie wie**, że to był outlier oportunistyczny.

**Doprecyzowanie zachowania użytkownika:** LOAD oportunistyczny przenoszę tylko gdy **warto** — **niska cena importu** *oraz* **wysoka produkcja PV** (tanio z sieci + „za darmo” z dachu). *Jeśli nie ma czego zużywać (drogo / ciemno), nie zużywam* — więc wysoki LOAD w historii przy **drożej** godzinie lub **bez PV** to raczej baza domu, a przy **tanio + dużo PV** — kandydat do wykluczenia z p50.

---

## LOAD oportunistyczny (kategoria)

Świadome przesuwanie dużych odbiorników na **korzystne** godziny:

- ładowanie EV
- zmywarka, pralka
- pompa ciepła, klimatyzacja
- inne duże, elastyczne obciążenia

To **nie** jest stały profil doby — w telemetrii widać piki w godzinach, które **jutro mogą nie powtórzyć się**, bo jutro nie będzie tak samo tanio / słonecznie.

### Gdzie to robić — warstwy

| Warstwa | Rola |
|---------|------|
| **Telemetria (wzbogacenie)** | Przy zapisie lub post-process: tagi godzin (`load_spike`, `opportunistic_candidate`, ewent. `ev_charge`). Źródło prawdy o „co się stało”. |
| **Load forecast (filtrowanie próbek)** | Przy medianie historycznej: **nie liczyć** lub obniżyć wagę próbek oznaczonych jako oportunistyczne. |
| **Planer (`load_plan`)** | `load_base` + opcjonalnie `load_shiftable_max` — optimizer nie zakłada przeniesionego LOAD, dopóki sam nie „kupi” taniej godziny. |

Sensowny kierunek: **sygnał przy telemetrii + decyzja w prognozie** (prognoza czyta tagi albo reguły na historycznych `(data, h)`).

### Czy da się odczytać „stare ceny” przy prognozie?

**Tak, częściowo — dziś w repo:**

| Sygnał | Skąd dla **przeszłej** daty telemetrii |
|--------|----------------------------------------|
| **Import PLN/kWh** | Taryfa G12 (`tariff_g12`) — strefa dzienna/nocna per **godzina** (powtarzalna co tydzień; nie „ta konkretna sobota była tańsza na RCE”). |
| **RCE / eksport** | Cache `data/pricing/rce_{YYYY-MM-DD}.json` (`pse_rce.get_or_fetch_hourly_rce_pln_per_kwh`) — **24 h RCE dla dnia dostawy**, jeśli kiedyś pobrane. |
| **PV w tej godzinie** | Telemetria `pv_w` / energia PV; historia Solcast w `pv_forecast` (proxy). |

Przy **łączeniu** rekordu telemetrii `(local_date, local_hour)` można więc ocenić: `import_pln(h)` + `rce[date][h]` + `pv_kwh` — i zapytać: *czy ten wysoki LOAD był w „oknie oportunistycznym” (tanio + PV)?*

**Czego jeszcze nie ma:** jednej funkcji „cena + PV dla każdej godziny historii” ani tagów w JSONL — trzeba by złożyć z G12 + cache RCE + telemetrii.

### Kierunki do przemyślenia (model)

1. **Reguła klasyfikacji historycznej:** `opportunistic = (load_kwh > base_threshold) AND (import_pln ≤ p25_strefy OR tania_godzina) AND (pv_kwh ≥ próg)` — progi do kalibracji.
2. **Wykrywanie EV / dużych pików** — osobna etykieta (moc × czas, import mimo PV, powtarzalność w soboty).
3. **Filtrowanie p50:** próbki z `opportunistic=True` wypadają z mediany lub dostają wagę 0.x.
4. **Model:** `load_base[h]` (prognoza bez przenoszenia) + `load_shiftable` tylko gdy **planowana** godzina jutro też tania + dużo PV (optimizer decyduje, nie historia 1:1).

**Status:** pomysł — wymaga specyfikacji progów i czy klasyfikacja idzie offline (batch po telemetrii) czy online co dzień przed `forecast_load_hours`.

---

## Klasyfikator: `load_base` vs `load_shiftable` (ułamek ∈ [0, 1))

**Pomysł:** dla każdej godziny (prognozy lub próbki historycznej) klasyfikator zwraca **`shiftable_fraction`** — jaka część LOAD jest *potencjalnie przenośna*, reszta to **baza**.

- **`shiftable_fraction ∈ [0, f_max]`**, typowo **`f_max < 1`** (np. 0.7–0.9) — zawsze zostaje standby: serwer, inwerter, lodówka, router itd.
- **`load_base_kwh ≈ load_kwh × (1 − shiftable_fraction)`** (lub osobno estymowany floor — patrz niżej).
- **`load_shiftable_kwh ≈ load_kwh × shiftable_fraction`** — tylko ta część może „zniknąć” z p50 po filtrze lub trafić do optimizera jako opcjonalna.

### Floor bazowy (problem p25 ≈ 0)

Dziś load forecast ma **p25 / p50 / p75**, **bez p10**. Przy mało próbek albo `no_history` wszystko = **0** — fizycznie nierealne (dom + Docker + inwerter).

**Kierunek:** globalny **`load_standby_floor_kwh/h`** (np. z nocnych godzin 2–5, mediany minimum, albo stała ~0.15–0.3 kWh/h z kalibracji) i:

```text
load_p25_eff = max(load_p25, floor)
load_p50_eff = max(load_p50, floor)   # opcjonalnie tylko dolne pasma
```

To osobny sygnał od shiftable — floor to *minimum fizyczne*, shiftable to *nadwyżka nad bazą behawioralną*.

### Heurystyki klasyfikatora (do łączenia, nie jedna reguła)

| Sygnał | Wskazuje na shiftable ↑ | Pułapka |
|--------|-------------------------|---------|
| Wysoki LOAD **tylko** w tym dniu, inne dni tygodnia o tej samej godzinie niskie | EV / jednorazowe | **Grzejnik do kąpieli** — patrz niżej |
| Tanio import + dużo PV w tej godzinie | świadome przesunięcie | bez PV / drogo → obniż shiftable |
| Pik >> mediana slota, **zero** podobnych pików w lookback | outlier oportunistyczny | — |

**Przykład nawyku (nie shiftable): grzejnik łazienkowy**

- **2–3 razy w tygodniu** (nie „raz na 2–3 dni” — to nadal regularny nawyk).
- **Sezonowy:** sensowny tylko **zimą**; latem próbki przy tej godzinie są niskie → prognoza na ciepły sezon **nie powinna** ciągnąć zimowego p75.
- **Nie przenośny** — stała godzina / przyzwyczajenie, nie reakcja na tanio + PV.

Dla klasyfikatora: **`recurrence_7d ≥ 2`** przy podobnej energii + ten sam slot → **habit → shiftable ↓**. EV w jedną sobotę: **`recurrence_7d ≈ 0–1`** + tanio + PV → **shiftable ↑**.

**Habit vs opportunistic:** sama reguła „nie w inne dni tygodnia” **nie wystarczy** (grzejnik i tak nie codziennie). Lepiej: **częstość w tygodniu**, **sezon** (temperatura zewn. / miesiąc / brak zdarzenia przez N tygodni), **tanio + PV** tylko przy kandydacie oportunistycznym.

Implementacja może być prosta na start (reguły + progi), później DSPy / mały model na cechach: `(load, delta_vs_median, hits_same_hour_7d, hits_same_hour_season, import_pln, pv_kwh, outdoor_temp_or_month)`.

### Co jest **już dziś** w algorytmie (częściowo)

| Mechanizm | Efekt | Czego **nie** robi |
|-----------|--------|---------------------|
| **Mediana p50** z próbek tej samej godziny | jeden outlier (np. EV w 1 sobotę) **słabo** podbija medianę vs średnia | nie odróżnia EV od grzejnika 2–3×/tydz. zimą; **brak sezonowości** (zimowy p75 w lecie) |
| **Split weekday / weekend** (`≥5` próbek) | inny profil sob–nied vs pn–pt | nie wie o „tanio + PV” |
| **p25 / p75** | pas niepewności (backtest coverage) | planer bierze **tylko p50** (`planner/inputs.py`) |
| **Nowcast** | skala względem bieżącego LOAD | nie dzieli base/shiftable |
| **Fallback** `global` / `no_history` | konserwatywne zero | właśnie źródło „p25 = 0” |

**Podsumowanie:** obecny algorytm to **robustna mediana + pasma + nowcast** — to *delikatnie* tłumi pojedyncze piki, ale **nie ma** klasyfikatora shiftable, floor standby, ani filtra tanio+PV. Grzejnik 2–3×/tydz. zimą **wchodzi** do mediany jak normalna próbka.

### Lookback 28 dni — zostaje

Rozważane było skrócenie do 14–21 d (szybsze wygaszenie sezonu / EV sprzed miesiąca). **Decyzja:** **4 tygodnie (`PLANNER_LOAD_LOOKBACK_DAYS = 28`) zostają** — zmiana sezonu i tak jest **przejściowa** (grzejnik stopniowo znika z próbek), a dłuższe okno daje **więcej próbek** na slot i stabilniejszy split weekday/weekend.

Skrócenie lookback **nie priorytet**; sensowniejsze docelowo: klasyfikator shiftable + ewent. floor standby, nie cięcie okna.

---

## Planer stochastyczny: ostrożność **całą dobę** (EV)

**Problem:** optimizer dziś widzi tylko **PV p50** i **load p50** (`planner/inputs.py`); p10/p90 PV to metryki UI. Jedna trajektoria = zakładamy, że prognoza trafia — bez „co jeśli się myli?”.

**Cel:** nie tylko floor SOC rano (skrót), lecz **każda godzina horyzontu** oceniana z perspektywy niepewności — bez wcześniejszego ładowania do pełna „na wszelki wypadek” (p90 musi też wchodzić do celu).

### Model (docelowy)

- **Decyzja (wspólna):** `ch_h`, `dis_h` — jedna fizyczna bateria.
- **Scenariusze** `s` z pełnymi profilami na **cały** horyzont, np.:
  - optymistyczny: PV p90, load p50
  - bazowy: PV p50, load p50
  - pesymistyczny: PV p10, load p75
- Dla stałej decyzji baterii import/eksport w scenariuszu `s` wynika z bilansu (residual po PV, load, ch, dis).
- **Cel:**

```text
max  Σ_s  π_s × Σ_h  cashflow_s(h)  −  wear(ch, dis)
```

Opcjonalnie: `max E[cashflow] − λ × CVaR_α(−cashflow_dzienny)` — jedna gałka ostrożności **globalnie**, nie per godzina rana.

### Balans (żeby nie być totalnym pesymistą)

| Mechanizm | Rola |
|-----------|------|
| Wagi `π_s` (np. 0,15 / 0,70 / 0,15) | p10 karze, p90 nagradza oszczędność SOC |
| Wear baterii | już w MILP — hamuje „ładuj wszystko na zapas” |
| Ceny per h | droga godzina sama podbija koszt złego scenariusza w tej h |

### Relacja do rezerwy nocnej

Rezerwa nocna w Guardianie = **airbag** przy deterministycznym planie p50. Stochastic planner **całą dobę** powinien robić większość pracy; po wdrożeniu i kalibracji — obniżyć / usunąć rezerwę (patrz sekcja wyżej).

### Wdrożenie w repo

1. Rozszerzyć `build_hour_inputs` o pasma PV (p10/p90) i load (p75).
2. `optimize_horizon_scenarios(...)` — ten sam MILP, bilans/cashflow × S scenariuszy w funkcji celu.
3. Backtest wag `π_s` na `reconcile` / `day_review`.

**Status:** pomysł normatywny na przyszłość — **nie** zaimplementowane.

---

## Powiązane pliki (gdy będzie implementacja)

| Obszar | Pliki |
|--------|--------|
| Load forecast | `load_forecast.py`, `docs/planner/modules/load_forecast.md` |
| Ceny historyczne | `energy_pricing.py`, `pse_rce.py` (`data/pricing/rce_*.json`), `tariff_g12.py` |
| Wejścia planera | `planner/inputs.py` |
| Telemetria | `data/telemetry/`, `planner/telemetry.py` |
| Rezerwa nocna | `guardian_logic.py`, `guardian_watchdog_override.py` |
| Optimizer | `planner/optimizer.py`, `planner/inputs.py` |

---

*Ostatnia aktualizacja: stochastic planner (EV cała doba); lookback 28 d; klasyfikator shiftable.*
