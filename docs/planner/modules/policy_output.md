# policy_output

**Wejście:** `PlannerDecision`, SOC, wynik korekty PV (metryki).

**Wyjście:** JSON (`schema_version`, `computed_at`, `valid_until`, `policy`, `params`, `metrics`, `degraded`).

| `policy` | Sens |
|----------|------|
| `charge_balance_zero` | Ładuj, bilans ~0 |
| `charge_balance_ignore` | Ładuj, bilans drugi |
| `charge_to_soc` | Ładuj do `target_soc_pct` |
| `discharge_to_soc` | Rozładuj do `target_soc_pct` |
| `balance_zero_only` | Tylko bilans zerowy |

**Mapowanie:** jeden wiersz z tabeli z wektora `e_bat` (reguły deterministyczne + testy). `degraded` przy brakach danych wg jednej tabeli w kodzie.
