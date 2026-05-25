# load_forecast

**Wyjście:** `load_p50[h]` [kWh/h] na godziny horyzontu (jak lista cen).

**Korekta:** jeden **`factor`** z krótkiego okna telemetrii vs baseline (brak danych → **1,0**); `load_p50 *= factor` na całym horyzoncie.

**Sloty:** +kWh w zadeklarowanych godzinach (jeden format w repo).
