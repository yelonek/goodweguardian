# pv_correction

**Norma:** [PLANNING_SYSTEM.md](../../../PLANNING_SYSTEM.md) §12 pkt 6.

**Implementacja:** `planner/pv_correction.py` → `planner/inputs.py`.

## Idea

Korekta **krótkoterminowa** (`k_intra`) na podstawie telemetrii bieżącej godziny vs prognoza Solcast p50. Nie ma globalnego mnożnika na cały dzień — korekta **przesuwa się** z zegarem.

## Wejście

- `now` (strefa telemetrii)
- Solcast `pv_kw` (p50) per slot
- Telemetria: `pv_w` od początku bieżącej godziny lokalnej
- **ε**, **k_min**, **k_max** (const w `planner/pv_correction.py`)

## Wyjście

- `pv_plan` per slot do optimizera
- Metadane w `inputs_snapshot.pv_correction` (audyt, dashboard)

## Algorytm

**α** = `(minuta + sekunda/60) / 60` — ułamek **bieżącej** godziny (nie od północy).

**A_so_far** = energia PV [kWh] od `:00` bieżącej godziny (suma `pv_w/1000/60` po próbkach).

**F_elapsed** = `α × F50_current`.

Gdy **F_elapsed > ε × α**:

```
k_raw = A_so_far / F_elapsed
k_intra = clip(k_raw, k_min_eff, k_max_eff)
```

**Dynamiczny clip** (domyślnie włączony): granice rozszerzają się z α (ułamek godziny):

| α | k_min_eff | k_max_eff |
|---|-----------|-----------|
| ≤ 0.15 | 0.65 | 1.35 (stały) |
| 0.55+ | 0.20 | 3.00 (szeroki) |

Pomiędzy — interpolacja liniowa (`PV_CORRECTION_CLIP_RAMP_*` w kodzie).

Inaczej: brak korekty (`k_intra = None`, surowy Solcast).

### Sloty

| Slot | pv_plan |
|------|---------|
| **bieżąca h** | `A_so_far + (1−α) × F50 × k_intra`; opcjonalnie blend z `recent_kw × (1−α)` |
| **h+1** | `k_intra × F50` |
| **h+2…** | `F50` (Solcast) |

**Rate blend:** `plan = (1−w)×k_intra_plan + w×rate_plan`, `w` rośnie od α=0.2 do 0.7.

Domyślnie: **ε = 0,1 kWh/h**, clip **0.65–1.35** (dynamicznie rozszerzany), rate window **15 min**.

Wyłączenie: `PV_CORRECTION_ENABLED = False` w `planner/pv_correction.py` (const; nie ma pokrętła w UI).

## Pasma reszty godziny (p10 / p90 → scenario MILP)

W środku bieżącej godziny [`planner/hour_remainder.py`](../../planner/hour_remainder.py) buduje **pełną godzinę conditioned**: `so_far +` pasma reszty z `pv_remainder_bands_kwh()` / `load_remainder_bands_kwh()`. `hour_fraction` idzie do MILP **tylko** jako limit mocy. Load: [`load_correction.md`](load_correction.md).

**Wejście (PV):** pełne pasma godziny (p50 skorygowany `k_intra`, p10/p90 × `k_scale`), `A_so_far`, `α`, opcjonalnie `recent_kw` (15 min).

**Algorytm (skrót):**

1. **Floor całej h:** `P10_total = max(p10, A_so_far)` (analogicznie p50/p90).
2. **Zwężanie:** `u = 1 − α`; `P50_rem = P50_total − A`; pasmo reszty wokół p50 o połowie szerokości `(P90_total − P10_total) × u / 2`.
3. **Floor z tempa:** gdy `recent_kw > 0` i `α ≥ 0,15`: dolna/górna granica reszty z `recent_kw × (1−α) × 0,70 / 1,15`.
4. **Kolejność:** `p10 ≤ p50 ≤ p90`.

Stałe: `PV_BAND_NARROW_ENABLED` (kill-switch), `PV_BAND_RATE_P10_FACTOR`, `PV_BAND_RATE_P90_FACTOR`, `PV_BAND_RATE_MIN_ALPHA`.

Efekt: pesymistyczny scenariusz reszty slotu nie zeruje PV, gdy słońce już jedzie — lepsze dane dla scenario MILP (wspólne ładowanie baterii z nadwyżki PV).

## Przykład

11:30, F50 = 2 kWh/h, A_so_far = 0,125 kWh (250 W średnio):

- α = 0,5, F_elapsed = 1,0 kWh
- k_intra = 0,125 / 1,0 → clip → **0,65**
- pv_plan(11h) = 0,125 + 0,5 × 2,0 × 0,65 = **0,775 kWh**
- pv_plan(12h) = 0,65 × F50_12h

## Świadomie poza zakresem / eksperymenty

- Globalne okno 3 h (`k` z wcześniejszej specyfikacji) — **nie** implementowane.
- **OWM Tier1 (eksperymentalne):** [`planner/pv_weather_correction.py`](../../planner/pv_weather_correction.py)
  skaluje Solcast na **h+2…h+6** przez `k_wx` z **Free** Current + 5-day/3h
  (`clouds`/`pop`/`weather`/`rain`; kroki 3h → godziny lokalne). Bez uvi/minutely.
  Wymaga `OPENWEATHER_API_KEY` + lat/lon; snapshot w `inputs_snapshot.pv_weather_correction`.
  Tier2 (temp/wilgotność) — później.
- `u` (p10/p50/p90) — pasma reszty bieżącej h wpływają na scenario MILP (patrz sekcja wyżej);
  pasma h+2… po OWM dziedziczą `k_scale` z `planner/inputs.py` gdy slot jest w `pv_corrected`.
