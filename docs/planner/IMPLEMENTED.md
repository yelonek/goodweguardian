# Stan implementacji planera

Opis faktycznego kodu. Norma produktu i kontrakt Guardiana: [PLANNING_SYSTEM.md](../../PLANNING_SYSTEM.md).

## Przepływ

1. `planner.service.build_rolling_plan()` buduje horyzont od bieżącej godziny do ostatniej godziny z parą cen RCE + import (zwykle dziś + jutro).
2. `planner.inputs` zapisuje prognozy, pasma, nowcast i pełne `HourInputs` w `inputs_snapshot`.
3. Wieloscenariuszowy solver tworzy 25 światów PV/load. Bieżąca decyzja `charge/discharge/import/export` jest wspólna, a przyszłe przepływy i SOC są per scenariusz.
4. Cel to `min E(import_cost − export_revenue + battery_wear)`, bez CVaR i bez wartości końcowej SOC.
5. `planner.policy_output` publikuje wykonalną decyzję bieżącego kroku do `state/planner_output.json`.
6. Guardian co minutę wykonuje ją na podstawie live PV, load, SOC i godzinowego net. Artefakt jest ważny 10 minut.

## Solver

- Implementacja: `planner/scenario_optimizer.py`.
- Jedna zmienna `charge`; optimizer nie przypisuje umownego pochodzenia każdej kWh.
- Non-anticipativity dotyczy wyłącznie slotu wykonywanego przed następnym solve (`h=0`).
- Przyszłe `soc`, `charge`, `discharge`, `import` i `export` są adaptacyjne per scenariusz.
- Bieżąca godzina używa zwiniętego nowcastu; przyszłe godziny zachowują pasma 5×5.
- Sprawność: `soc[h+1] = soc[h] + √η_rt·charge − discharge/√η_rt`.
- Arbitraż sieć–bateria–sieć jest dozwolony, jeśli spread pokrywa sprawność i wear.
- Nie ma `green_stock` ograniczającego późniejszy eksport energii kupionej z sieci.
- Nocna zapadka 22–5 zostaje: po istotnym ładowaniu brak eksportu do końca okna. Eksport od 6:00 jest dozwolony.
- `planner/optimizer.py` zawiera deterministyczny MILP p50 jako fallback po błędzie solvera.

Stary `shared_battery_grid_recourse` jest zachowany tylko jako kontrola trybu shadow. Tracking-SP ze wspólnym `soc*` i karą tracking nie jest zaimplementowany ani używany.

## Kontrakt policy

`HourPlan` zapisuje:

- `target_net_kwh` i `target_net_remainder_kwh`,
- `planned_charge_kwh` i `planned_discharge_kwh`,
- oczekiwany `soc_start_pct` / `soc_end_pct`.

`HourPolicyParams` dodaje:

- `allow_grid_charge`,
- `grid_charge_budget_kwh`,
- `max_additional_export_kwh`,
- jawne planowane charge/discharge.

Guardian najpierw wykorzystuje live PV (`charge_pv`). Aktywne uzupełnienie z sieci (`charge_grid`) jest ograniczone budżetem i targetem SOC. Po osiągnięciu planowanego limitu eksportu dodatkowe PV jest kierowane do baterii, zamiast automatycznie zwiększać eksport.

## Audyt scenariuszy i dashboard

`ScenariosDetail` zapisuje dla każdego świata oddzielne:

- `soc_pct`,
- `charge_kwh`,
- `discharge_kwh`,
- `net_kwh`,
- `cashflow_hour_pln`.

Dashboard pokazuje oczekiwaną trajektorię SOC oraz pasma przepływów scenariuszy.

## Tryby wdrożenia

Ustawienie `planner_optimizer_mode`:

- `legacy` — steruje dawny shared-battery MILP,
- `shadow` — steruje legacy, MPC liczy te same wejścia i zapisuje osobny artefakt,
- `stochastic_mpc` — nowy solver publikuje policy.

Domyślnie jest `shadow`. Implementacja nie przełącza istniejącego override ani nie włącza nowego sterowania.

Artefakty:

- plan produkcyjny: `data/planner/plans/plan_latest.json`,
- kandydat: `data/planner/plans/plan_shadow_latest.json`,
- porównanie: `data/planner/plans/comparison_latest.json`.

Porównanie obejmuje pierwszą decyzję, oczekiwany PLN, zmiany trybu, drogi import i nocne cykle:

```bash
uv run python -m planner compare
```

Konfiguracja `Planner: compare shadow` w `.vscode/launch.json` uruchamia ten sam entry point przez `debugpy`.

## Najważniejsze pliki

- `planner/scenario_optimizer.py` — stochastic MPC i kontrolny solver legacy,
- `planner/optimizer.py` — routing i deterministyczny fallback,
- `planner/policy_output.py` — mapowanie przepływów na policy,
- `guardian_execution.py` — wykonanie celu przy live telemetry,
- `planner/shadow_compare.py` — odtwarzalne porównanie,
- `planner/service.py` — rolling solve, zapis planów i policy.
