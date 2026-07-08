# Pomysły na przyszłość

Notatnik roboczy — **nie norma produktowa**. Szczegóły wdrożenia dopiero po przemyśleniu i testach.

---

## Mapa pomysłów (podsumowanie)

Trzy niezależne warstwy — wspólne źródło: telemetria + cennik (G12, RCE).

```text
Historia (TWC)              Przyszłość (deklaracja)           Raport (dashboard)
─────────────────           ───────────────────────           ──────────────────
load_base = total − EV  →   load_plan = base + ev_slot    →   piramida PV × RCE
       ↓                            ↓                      →   dzień/tydz./mies. + cashflow
   lepsze p50                 planer + rekomendacja godzin
```

| # | Pomysł | Warstwa | Dla kogo | Status |
|---|--------|---------|----------|--------|
| 1 | **`load_base`** — odjąć EV od prognozy loadu (+ korekta dzienna) | `load_forecast.py` | planer | **wdrożone** |
| 2 | **Piramida PV × RCE** — skumulowane progi &lt;10…&lt;60 gr | dashboard / KPI | **użytkownik** | **wdrożone** (UX) |
| 2b | **Zużycie dzień/tydz./mies.** — dom vs EV + cashflow | dashboard / KPI | **użytkownik** | pomysł (UX; po agregacji `load_base`) |
| 3 | **Rekomendacja godzin EV** — „N kWh dziś” → sloty | dashboard + planer | użytkownik + planer | **wdrożone** |
| 4 | **`load_plan = base + ev`** w optimizerze | `planner/` | planer | **wdrożone** (post-processing load) |

**Kolejność wdrożenia:** 1 → 2 i 2b równolegle (KPI, bez planera) → 3 → 4.

**Zasada EV:** ładowanie oportunistyczne tylko gdy **tanio import** *i* **dużo PV** — patrz sekcja LOAD oportunistyczny. TWC daje twardy pomiar historii; deklaracja użytkownika — intencję na przyszłość.

---

## Planer: moduł zawsze w GUI, przełącznik „planuj / utrzymuj bilans 0”

**Kontekst:** dziś włączanie/wyłączanie planera robi `planner_control.effective_planner_execution_enabled()` — guardian czyta policy z `state/planner_output.json` albo wpada w fallback (`decide_watchdog`, Flappy ~0). Przełącznik jest poza GUI; wyłączony planer = brak modułu planowania w interfejsie.

**Olśnienie:** moduł planowania **zawsze obecny w GUI**, niezależnie od stanu przełącznika. Różnica tylko w treści pól, które moduł pokazuje:

| Stan przełącznika | Co w polach planera (GUI) |
|-------------------|---------------------------|
| **Włączony** | faktyczny plan: `exec_mode`, `target_net_kwh`, `soc_end_pct`, `battery_delta_kwh` z MILP |
| **Wyłączony** | **wszystkie pola = „utrzymaj bilans 0 kWh”** — `exec_mode = neutral`, `target_net_kwh = 0`, `battery_delta_kwh = 0`, `soc_end_pct = aktualny` |

**Skutek architektoniczny:** „wyłączony planer” nie znaczy „guardian sam sobie radzi bez policy”, tylko **policy = sztuczny plan neutralny dla każdej godziny**. Guardian (albo docelowy executor — patrz dyskusja o rozdzieleniu guardiana) zawsze dostaje `HourPolicyRow` i zawsze wykonuje ten sam kod — tylko przy wyłączonym planerze wiersz mówi „trzymaj zero”.

**Zalety:**
- Jedna ścieżka wykonawcza (żadnego rozdwojenia `if policy_active → decide_plan_execution else → decide_watchdog` w `hourly_balance_run.py:383–399`).
- GUI pokazuje moduł planowania zawsze — widać, *co by planer mówił*, gdyby był włączony (na szaro / z badge „wyłączony”), a obok sztuczny „utrzymuj 0”. Decyzja użytkownika świadoma, nie ukryta.
- Włączenie/wyłączenie = zmiana treści pól, nie gaśnięcie modułu. Łatwiejsze A/B i debug: ten sam pipeline telemetrii/audytu działa w obu stanach.
- Semantyka `target_net_kwh` przestaje być polimorficzna przez tryb (problem z §13.4) — przy wyłączonym planerze zawsze `0`, czyli jawny Flappy ~0.

**Wady / do przemyślenia:**
- Trzeba jawnie generować „sztuczny policy artifact” dla każdej godziny horyzontu (albo jeden wiersz na bieżącą godzinę — wystarczy, bo guardian i tak czyta per `now.hour`).
- Obrony SOC nadal muszą mieć pierwszeństwo nad sztucznym `neutral` — to i tak już prawda (`decide_soc_defenses` przed `exec_mode` w `guardian_execution.py:171`).
- Auditor/telemetria widzi `exec_mode=neutral, plan_id=<syntetyczny>` — trzeba oznaczyć, żeby nie mylić z realnym planem neutralnym (np. `plan_id="off:balance0"`).

**Relacja do dyskusji o architekturze:** pasuje do propozycji rozdzielenia guardiana (watchdog bilansu + SOC) od executora (translate `exec_mode` → slot). Przy wyłączonym planerze executor dostaje `neutral` i schodzi do guardian-balance; przy włączonym — pełny plan. **Jedne drzwi, dwa tryby treści, nie dwie pętle kodu.**

**Status:** pomysł — do wdrożenia razem z ewentualnym refaktorem executor/guardian; sam syntetyczny `neutral=0` można dodać niezależnie jako sanity check przed refaktorem.

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
2. **Wykrywanie EV** — **TWC** (`Δ E_twc` &gt; ε) zamiast heurystyki „pik >> mediana”; fallback heurystyczny tylko bez danych TWC w historii.
3. **Filtrowanie p50:** próbki z `opportunistic=True` wypadają z mediany lub dostają wagę 0.x.
4. **Model:** `load_base[h]` (prognoza bez przenoszenia) + `load_shiftable` tylko gdy **planowana** godzina jutro też tania + dużo PV (optimizer decyduje, nie historia 1:1).

**Status:** pomysł — heurystyka tanio+PV nadal do rozważenia dla *innych* odbiorników; **EV rozstrzyga TWC** + `load_base` (sekcja niżej).

---

## Load forecast: `load_base` (odjęcie EV)

**Cel:** prognoza p50 opisuje **dom bez przypadkowych sesji ładowania**, nie sumę `consumption_w` z inwertera.

**Problem:** mediana z ostatnich 28 dni wciąga piki EV (np. sobota, tanio + PV) — planer przeszacowuje load (patrz sekcja wyżej).

### Wzór (agregacja godzinowa)

```text
load_total_kwh[h]  = avg(consumption_w) / 1000          # jak dziś
ev_charge_kwh[h]   = Δ E_twc w godzinie H               # z telemetrii TWC
load_base_kwh[h]   = max(0, load_total − ev_charge)     # próbka do mediany
```

Percentyle **p25 / p50 / p75** liczyć z `load_base`, nie z `load_total`.

### Lookback: **nie** wyrzucać całych dni z ładowaniem

Przy poprawnym `load_base[h]` per godzina **nie ma** powodu usuwać „dni ładowania” z lookbacku.

Przykład: sobota, EV 10–14. Godziny 10–14 → próbka bazy (bez EV); godziny 7, 20 itd. → normalne próbki dla slotów 7, 20. Mediana slotu 7:00 **nie jest skażona** — EV było w innych godzinach. Wykluczenie całego dnia **usuwa też dobre próbki**.

| Sytuacja | Lookback |
|----------|----------|
| Godzinowe `load_base` z TWC | **Zostaw dzień** — próbki = `load_base[h]` |
| Brak TWC / nie da się odjąć EV w godzinie | fallback `load_total[h]` (jak dziś) lub pomiń tylko tę godzinę |
| Błędne rozsmarowanie `ev_day/24` | **nie** — EV odejmować tylko w godzinach z Δ TWC |

Wykluczenie **całego** dnia — wyłącznie gdy **żadnej** wiarygodnej godziny `load_base` nie da się zbudować (rzadki edge case w historii przed TWC).

### Implementacja

- Agregacja `delta_twc_kwh` / licznik `E_twc_kwh` per godzina (jak Δ `E_pv_kwh` w dashboardzie).
- Zmiana `_hourly_kwh_from_file` / cache w `load_forecast.py`.
- Historia sprzed wdrożenia TWC: brak `E_twc_kwh` → fallback do `load_total` (zachowanie jak dziś).
- Testy: backtest coverage; dzień z ładowaniem EV **nie podbija** p50 slotów bez ładowania (przy godzinowym `load_base`).

**Status:** **następny priorytet** po telemetrii TWC.

### Korekta prognozy z `load_base_day` (druga warstwa)

Godzinowa mediana mówi **kształt** doby; suma dzienna mówi **poziom** — to uzupełnia, nie zastępuje `load_base[h]`.

```text
load_base_day = Σ_h load_base_kwh[h]   # = load_total_day − ev_day
```

**Nie** służy do filtrowania lookbacku (patrz wyżej). Służy do:

| Mechanizm | Opis | Priorytet |
|-----------|------|-----------|
| **Nowcast na bazie** | `base_so_far = Σ load_base` (godziny zakończone); `factor = base_so_far / Σ p50_base`; skala pozostałych slotów — zamiast surowego `consumption_w` (miesza EV) | po `load_base[h]` |
| **Kształt × poziom** | `shape[h] = p50[h] / Σ p50`; `level_day` = mediana `load_base_day` (ten sam typ dnia); `forecast[h] = shape[h] × level_day` | jeśli backtest pokaże błąd poziomu (sezon) |
| **Sanity sumy dobowej** | `Σ forecast[h]` w pasie z historii `load_base_day` (p25–p75) | opcjonalnie |
| **Plan z deklaracją EV** | `load_plan[h] = forecast_base[h] + ev_scheduled[h]` — korekta dzienna tylko na **bazie**, EV osobno | po rekomendacji EV |

Kolejność wdrożenia korekt: (1) `load_base[h]` w medianie → (2) nowcast na `base_so_far` → (3) shape×level po backteście → (4) deklaracja EV w planie.

**Pułapki:** nie odejmować EV drugi raz na poziomie dnia, jeśli próbki są już `load_base[h]`; intraday korekta tylko z godzin `complete`.

---

## EV: rekomendacja godzin ładowania

**Cel:** użytkownik podaje **ile kWh chce naładować dziś** → system proponuje **godziny**; planer widzi `load_plan[h] = load_base_p50[h] + ev_charge[h]`.

### Zachowanie użytkownika (założenie)

Ładuję tylko gdy **warto**: niska cena importu (G12 noc / 13–15) **oraz** dużo PV. Bez deklaracji — planer zakłada tylko `load_base` (bez EV).

### MVP (bez pełnego MILP)

1. Formularz w dashboardzie: `ev_target_kwh` na dziś (opcjonalnie max moc ładowarki, np. 11 kW).
2. Ranking godzin horyzontu wg kosztu / korzyści: `import_pln[h]`, prognoza `pv_kwh[h]`, ewent. `rce[h]` (koszt „nie wyeksportowania” PV).
3. Greedy: pakuj `ev_target_kwh` w najlepsze sloty (limit kWh/h = moc × 1 h).
4. Wyjście: lista godzin + suma w planie; **rekomendacja** — TWC jest read-only, użytkownik włącza ładowanie w aplikacji Tesli / ręcznie.

### Pełna integracja (później)

- Zmienne `ev_h` w `scenario_optimizer` lub post-processing slotów.
- Spójność z SOC, export_profit, wear baterii.

**Status:** **wdrożone** (`load_forecast.py`, `ev_charging_plan.py`, dashboard Prognoza).

---

## Dashboard: piramida PV wg RCE (tylko UX)

**Cel:** informacja **dla użytkownika** — ile energii **wyprodukowało PV** w godzinach o danej (niskiej) cenie eksportu RCE. **Nie** wpływa na planer, Guardiana ani `load_forecast`.

### Intuicja

Gdy RCE jest niskie (np. &lt; 30 gr), eksport do sieci ma małą wartość — lepiej zużyć PV lokalnie (dom, EV). Warstwa **&lt; 60 gr** porównuje się z **taryfą dzienną ~59 gr** (druga strefa G12): PV w tych godzinach „tańsze” niż późniejszy import.

### Definicja — skumulowane progi

Dla wybranego dnia, dla każdego progu **RCE &lt; X gr** (X ∈ {10, 20, 30, 40, 50, 60}):

```text
pv_cum_kwh[< X gr] = Σ_h  pv_kwh[h]   gdzie   rce[h] < X/100 PLN
```

Progi **skumulowane** (połączone): &lt;20 gr zawiera &lt;10 gr itd. — łatwo dodać nowy próg na końcu (np. &lt;70 gr) bez przebudowy UI.

**Nie** chodzi o: import G12, load domu, autokonsumpcję vs eksport — tylko **produkcja PV × cena RCE w tej godzinie**.

### Źródła danych (już w repo)

| Sygnał | Skąd |
|--------|------|
| `pv_kwh[h]` | Δ `E_pv_kwh` z telemetrii (`guardian_dashboard` — ten sam wzorzec) |
| `rce[h]` | `pricing_day_breakdown` / cache `data/pricing/rce_{date}.json` |

### Prezentacja (propozycja)

Tabela lub stos (piramida) — od węższych progów do szerszych:

| Warstwa | kWh PV (przykład) |
|---------|-------------------|
| RCE &lt; 10 gr | … |
| RCE &lt; 20 gr | … |
| … | … |
| RCE &lt; 60 gr | … *(ok. taryfa dzienna 59 gr)* |

Opcjonalnie: osobna linia **RCE ≥ 60 gr** (eksport blisko opłacalny vs import).

**Status:** pomysł — moduł KPI + fragment dashboardu; można równolegle z `load_base`.

---

## Dashboard: zużycie dom vs EV + cashflow (dzień / tydzień / miesiąc)

**Cel:** czytelne podsumowanie **dla użytkownika** — ile energii zużył **dom** (bez EV), ile poszło na **ładowanie**, oraz **cashflow** z istniejącego KPI net billing. **Nie** wpływa na planer ani Guardiana.

### Dlaczego to nie jest redundantne

| Co już jest | Czego brakuje |
|-------------|----------------|
| KPI dzienny: bilans **sieci** (Δimp/Δexp), depozyt/rachunek PLN | **Brutto zużycia domu** [kWh] — inwerter `consumption_w`, nie liczniki importu |
| Load **godzinowy** (telemetria, prognoza p50) | Jedna liczba **na dzień**: dom / EV / razem |
| Prognoza = oczekiwanie | Raport = **fakt** wczoraj / ten tydzień / ten miesiąc |

Relacja do **shiftable:** `ev_day / load_total_day` — obserwowany udział loadu przenośnego; `load_base_day` — trend poziomu doby (UX i ewent. `level_day` w prognozie; **nie** do wyrzucania dni z lookbacku).

### Definicja — dzień kalendarzowy (00:00–00:00, `TELEMETRY_TZ`)

```text
load_total_day   = Σ_h  load_total_kwh[h]          # avg(consumption_w)/1000 per godzina
ev_day           = E_twc_kwh(koniec D) − E_twc_kwh(początek D)   # licznik lifetime TWC
load_base_day    = load_total_day − ev_day         # ≥ 0; dom bez auta
```

Równoważnie: `load_base_day = Σ_h load_base_kwh[h]` po agregacji godzinowej z TWC.

Dni bez pełnej telemetrii TWC: `ev_day` = null → pokazać tylko `load_total_day` (jak dziś) z adnotacją.

### Cashflow (już w `_kpi_for_day`)

Na ten sam dzień **obok** kWh:

| Pole | Znaczenie |
|------|-----------|
| `deposit_add_pln_day` | nadwyżka eksportu × RCE |
| `electricity_bill_pln_day` | nadwyżka importu × taryfa G12 |
| `net_cashflow_pln_day` | depozyt − rachunek |

Źródło: `guardian_dashboard._kpi_for_day` — **bez zmiany definicji**; nowe KPI zużycia to **osobny blok** obok finansów.

### Agregaty tygodnia i miesiąca

Sumy po dniach kompletnych (wszystkie 24 h telemetrii + TWC gdy włączone):

```text
week:  Σ load_base_day, Σ ev_day, Σ load_total_day, Σ net_cashflow_pln_day
month: j.w. dla zakresu kalendarzowego
```

Opcjonalnie w UI:
- **średnia / mediana** `load_base_day` — „typowy dzień domu” (wszystkie dni z kompletną telemetrią; dni z EV nadal liczą się po odjęciu `ev_day`);
- liczba **dni z ładowaniem** (`ev_day > ε`);
- **PV** wyprodukowane w okresie (suma Δ `E_pv`) — obok zużycia.

### Prezentacja (propozycja)

**Dzień:** karty `Dom · EV · Razem` [kWh] + `Cashflow` [PLN].

**Tydzień / miesiąc:** tabela lub wykres słupkowy — kWh (dom / EV) + linia cashflow skumulowany; ewent. porównanie z poprzednim okresem.

API: rozszerzenie `GET /api/kpi/day` + `GET /api/kpi/period?from=&to=` (lub week/month).

**Status:** pomysł — UX wysokiego priorytetu; implementacja po agregacji godzinowej `load_base` (wspólny kod z `load_forecast`). Te same agregaty co warstwa korekty prognozy (`load_base_day`).

**Uwaga:** raport dzienny to **UX**; korekta prognozy czyta te same liczby — patrz sekcja **Load forecast: korekta z `load_base_day`**.

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

### Model

- **Per scenariusz:** `ch_s,h`, `dis_s,h`, `soc_s,h`, `imp_s,h`, `exp_s,h`.
- **Scenariusze** `s` z pełnymi profilami na **cały** horyzont:
  - optymistyczny: PV p90, load p50
  - bazowy: PV p50, load p50
  - pesymistyczny: PV p10, load p75
- Plan wykonawczy (Guardian): scenariusz **bazowy** (p50); rolling replan co 10 min.
- **Cel:**

```text
max  Σ_s  π_s × Σ_h  cashflow_s(h)  −  wear(ch_s, dis_s)
```

Wagi `π_s` (np. 0,15 / 0,70 / 0,15) oceniają ryzyko przez ważoną średnią cashflow.

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

**Status:** wdrożone w `planner/scenario_optimizer.py` (`PLANNER_SCENARIO_OPTIMIZER=1` domyślnie; `off` = p50).

---

## Optimizer: brak modelu eco-slotów (`import_grid` / DC vs AC)

**Kit na razie** — Guardian egzekwuje fizykę inwertera; **MILP** (`planner/optimizer.py`) tego **nie** odwzorowuje. Planer liczy ogólny bilans godzinowy, a `exec_mode` wybiera **po** optymalizacji z `(target_net_kwh, battery_delta_kwh)`.

### Fizyka GoodWe (`import_grid`, §13)

Przy **CHARGE 1%** i **celu slotu SOC = 10%**, gdy **SOC baterii > 10%**:

| Strumień | Skąd |
|----------|------|
| **LOAD** | **sieć** (AC) |
| **PV → bateria** | tylko DC („co łaska”) |
| **Sieć → bateria** | **nie** (slot blokuje AC→magazyn) |

Chcesz **ładować magazyn z sieci** → `charge_grid`, nie `import_grid`.

### Co robi optimizer dziś

Jeden równanie bilansu na godzinę (`imp − exp + dis − ch = load − pv`) — **bez**:

- progu slotu SOC (10% vs globalne `soc_min`/`soc_max`),
- rozdzielenia źródła `ch` (sieć vs PV),
- trybu eco-slot per godzina **przed** solve.

Optimizer **może** zaplanować `ch > pv` (ładowanie magazynu z importu) albo pokrycie LOAD z baterii — w `import_grid` to **niemożliwe**.

### Kiedy plan ≈ OK vs rozjeżdża się

| Sytuacja | Plan / mapper |
|----------|----------------|
| Noc, PV ≈ 0, `net < 0`, `battery_delta ≈ 0` | **`import_grid`** — sensowne |
| Dzień, PV > 0, LOAD z sieci, całe PV do baterii | Fizyka: `imp ≈ load`, `ch = pv`, `net ≈ −load` — mapper dziś często **`charge_grid`** (`battery_delta > 0`) |
| Optimizer: `bd > 0`, `net ≈ 0` | Mapper dawał **`charge_grid`** | **`neutral`** (Flappy soak z PV); `charge_grid` tylko gdy **`net < −ε`** (import do magazynu) |

### Kierunki na później (gdy wrócimy do optimizera)

1. **Ograniczenia per tryb** (albo pre-label godzin): w `import_grid` → `ch ≤ pv`, `imp ≥ load − ε`, zakaz `dis`.
2. **Mapper:** `(net < 0, ch ≈ pv, imp ≈ load)` → `import_grid`, nie `charge_grid`.
3. **SOC slot 10%** w trajektorii planera (osobno od `PLANNER_SOC_MIN_PCT`).
4. Ewentualnie **osobne zmienne** `ch_pv` / `ch_grid` w MILP.

**Status:** świadoma luka — **nie** naprawiamy teraz; Guardian i §13 wystarczają na produkcję.

---

## Telemetria: Tesla Wall Connector Gen 3 (`energy_wh`)

**Cel:** rozdzielić LOAD domu od ładowania EV — inwerter GoodWe widzi tylko sumę `consumption_w`; bez osobnego licznika EV wysoki LOAD w historii (np. sobota, tanio + PV) zafałszowuje medianę p50.

**Źródło:** lokalne HTTP API ładowarki — **tylko** `GET /api/1/lifetime`, pole `energy_wh` (Wh, monotoniczny licznik lifetime). **Bez** `/api/1/vitals` — do historii i tagu `ev_charge` wystarczą przyrosty `energy_wh`; częsty polling `vitals` bywa niestabilny (timeout po godzinach).

**Przykład odpowiedzi:**

```json
{
  "energy_wh": 12125146,
  "charge_starts": 3044,
  "charging_time_s": 6409212
}
```

Pozostałe pola `lifetime` (`charge_starts`, `charging_time_s`, `uptime_s`) **nie** są potrzebne do estymacji loadu.

### Pola w telemetrii (cykl minutowy)

| Pole | Znaczenie |
|------|-----------|
| `E_twc_kwh` | `energy_wh / 1000` |
| `delta_twc_kwh` | przyrost od startu lokalnej godziny (jak `delta_imp_kwh`) |

Opcjonalnie przy agregacji godzinowej: `ev_charging = delta_twc_kwh > ε`.

### Konfiguracja

- `TESLA_WC_HOST` (lub `TESLA_WC_IP`) — IP / hostname w LAN; puste = moduł wyłączony, pola telemetrii `null`.
- `TESLA_WC_TIMEOUT_S` — timeout HTTP (domyślnie 5 s).

### Zbieranie danych

- **Ten sam cykl** co inwerter (`hourly_balance_run.py`, ~1 min) — **nie** osobny cron (unikamy równoległego zapisu do JSONL).
- Jeden GET `/api/1/lifetime` na cykl.

### Docelowy wpływ na load forecast

```text
load_total_kwh  = avg(consumption_w) / 1000     # jak dziś
ev_charge_kwh   = Δ E_twc w godzinie
load_base_kwh   = load_total_kwh − ev_charge_kwh  # do mediany p50
```

Szczegóły: sekcja **Load forecast: `load_base`**.

Powiązanie z **LOAD oportunistyczny:** twardy pomiar EV; rekomendacja godzin — sekcja **EV: rekomendacja godzin ładowania**.

**Status:** **wdrożone** — moduł `tesla_wall_charger.py` + zapis w telemetrii + `load_base` w `load_forecast.py`.

---

## Powiązane pliki (gdy będzie implementacja)

| Obszar | Pliki |
|--------|--------|
| Load forecast / `load_base` | `load_forecast.py`, `docs/planner/modules/load_forecast.md` |
| Piramida PV × RCE (UX) | `guardian_dashboard.py` (KPI), `energy_pricing.py`, telemetria PV |
| Zużycie dom/EV + okresy | `guardian_dashboard.py` (`_kpi_for_day`, agregaty), telemetria, TWC |
| Rekomendacja EV | dashboard (formularz), `planner/inputs.py`, ewent. `planner/scenario_optimizer.py` |
| Ceny historyczne | `energy_pricing.py`, `pse_rce.py` (`data/pricing/rce_*.json`), `tariff_g12.py` |
| Wejścia planera | `planner/inputs.py` |
| Telemetria / TWC | `data/telemetry/`, `planner/telemetry.py`, `tesla_wall_charger.py`, `hourly_balance_run.py` |
| Rezerwa nocna | `guardian_logic.py`, `guardian_watchdog_override.py` |
| Optimizer / eco-slot | `planner/optimizer.py`, `planner/policy_output.py`, `guardian_execution.py` |

---

*Ostatnia aktualizacja: planer zawsze w GUI, przełącznik „planuj / utrzymuj bilans 0” (syntetyczny policy neutral); load_base — lookback z dniami EV (godzinowo, bez wyrzucania dni); korekta prognozy z load_base_day; raport zużycia + cashflow.*
