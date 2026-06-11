# load_forecast

**Wyjście:** `load_p50[h]` [kWh/h] na godziny horyzontu (jak lista cen).

**Korekta:** `factor = clip(recent_W / baseline_W, min, max)` z okna telemetrii vs p50 bieżącej godz. (brak danych → bez korekty). Slot `step`: `effective_factor = 1 + (factor − 1) × (1 − step/decay)`; percentyle × `effective_factor`.

**Sloty:** +kWh w zadeklarowanych godzinach (jeden format w repo).
