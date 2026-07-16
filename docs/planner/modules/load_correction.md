# load_correction

Zwężanie pasm load (p25/p50/p75) na **resztę** bieżącej godziny — lustrzane do [`pv_correction.md`](pv_correction.md) / `pv_remainder_bands_kwh`.

**Moduł:** [`planner/load_correction.py`](../../planner/load_correction.py)  
**Wiring:** [`planner/hour_remainder.py`](../../planner/hour_remainder.py) → full-hour conditioned bands (`so_far + rem`) do MILP.

## Wejścia

| Pole | Źródło |
|------|--------|
| `a_so_far_kwh` | suma `consumption_w` od :00 (`load_energy_so_far_in_hour`) |
| `recent_kw` | średnia moc load, okno 15 min |
| `α` | ułamek godziny |
| p50/p25/p75 full | `load_forecast` (+ EV na p75) |

## Algorytm

1. Floor: `P*_total = max(band, A_so_far)`
2. Zwężanie: `u = 1−α`; reszta wokół p50 o półszerokości `(p75−p25)×u/2`
3. Floor z tempa: `recent_kw × (1−α) × 0,70 / 1,15` (gdy α ≥ 0,15)
4. Kolejność: `p25 ≤ p50 ≤ p75`

Kill-switch: `LOAD_BAND_NARROW_ENABLED = False` → `max(0, band − A)`.

## Efekt

Pesymistyczny scenariusz (load p75) mid-hour nie zakłada „całego” p75 na resztę, gdy dom już dużo zużył — węższe pasma, mniej sztucznego hedge’u importem.
