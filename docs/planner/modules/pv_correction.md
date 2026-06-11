# pv_correction

**Norma:** [PLANNING_SYSTEM.md](../../../PLANNING_SYSTEM.md) §12 pkt 6.

**Implementacja:** `planner/pv_correction.py` → `planner/inputs.py`.

## Idea

Korekta **krótkoterminowa** (`k_intra`) na podstawie telemetrii bieżącej godziny vs prognoza Solcast p50. Nie ma globalnego mnożnika na cały dzień — korekta **przesuwa się** z zegarem.

## Wejście

- `now` (strefa telemetrii)
- Solcast `pv_kw` (p50) per slot
- Telemetria: `pv_w` od początku bieżącej godziny lokalnej
- **ε**, **k_min**, **k_max** (env: `PV_CORRECTION_*`)

## Wyjście

- `pv_plan` per slot do optimizera
- Metadane w `inputs_snapshot.pv_correction` (audyt, dashboard)

## Algorytm

**α** = `(minuta + sekunda/60) / 60` — ułamek **bieżącej** godziny (nie od północy).

**A_so_far** = energia PV [kWh] od `:00` bieżącej godziny (suma `pv_w/1000/60` po próbkach).

**F_elapsed** = `α × F50_current`.

Gdy **F_elapsed > ε × α**:

```
k_intra = clip(A_so_far / F_elapsed, k_min, k_max)
```

Inaczej: brak korekty (`k_intra = None`, surowy Solcast).

### Sloty

| Slot | pv_plan |
|------|---------|
| **bieżąca h** | `A_so_far + (1−α) × F50 × k_intra` |
| **h+1** | `k_intra × F50` |
| **h+2…** | `F50` (Solcast) |

Domyślnie: **ε = 0,1 kWh/h**, **k_min = 0,65**, **k_max = 1,35**.

Wyłączenie: `PV_CORRECTION_ENABLED=false`.

## Przykład

11:30, F50 = 2 kWh/h, A_so_far = 0,125 kWh (250 W średnio):

- α = 0,5, F_elapsed = 1,0 kWh
- k_intra = 0,125 / 1,0 → clip → **0,65**
- pv_plan(11h) = 0,125 + 0,5 × 2,0 × 0,65 = **0,775 kWh**
- pv_plan(12h) = 0,65 × F50_12h

## Świadomie poza zakresem

- Globalne okno 3 h (`k` z wcześniejszej specyfikacji) — **nie** implementowane.
- Prognoza pogody (OpenWeather itd.) — osobny moduł na horyzont 2–6 h, opcjonalnie później.
- `u` (p10/p50/p90) — tylko metryki UI, nie wpływa na optimizer.
