# pv_correction

**Norma liczb i okna:** [PLANNING_SYSTEM.md](../../../PLANNING_SYSTEM.md) §12 pkt 6.

**Wejście:** `now` (strefa telemetrii), faktyczne kWh/h + p10/p50/p90, **ε**, **k_min**, **k_max**.

**Wyjście:** `k`, `u | None`, opcjonalnie sumy okna i `correction_active`.

**`u` (tylko metryki):** jeśli `A ≤ F10` → strefa ≤p10; jeśli `A ≥ F90` → ≥p90; między F10–F50 i F50–F90 — interpolacja liniowa do pseudokwantyla w **0,1–0,5** i **0,5–0,9**; przy degeneracji (różnica &lt; **1e−9** kWh) → `u = None`.

**`correction_active`:** `True` gdy po obliczeniu `k` było **`F50 ≥ 10⁻⁶`** w oknie (sensowne porównanie).

Implementacja kroków okna / `k` = dokładnie §12 pkt 6.
