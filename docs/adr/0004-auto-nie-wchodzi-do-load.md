# Auto nie wchodzi do load

Zastępuje sposób z [0003](0003-planer-dostaje-budzet-ladowania-bez-zakazu-eksportu.md), w którym budżet ładowania był dopisywany do zużycia domu.

Load w planie i w kolumnie p50 Prognozy to zużycie domu. Rozkład auta zostaje w kolumnie EV. Godzina, w której slot auta jest większy niż nadwyżka PV, nie dostaje przez to importu z sieci — planer nie musi tej energii „obsłużyć” jak load.

## Considered options

- Zostawić auto w load i tylko nie pokazywać sumy w p50. Odrzucone: optimizer i tak zerował net i przy slocie większym niż nadwyżka godziny brał brak z sieci.
- Obciąć slot do nadwyżki godziny i dalej dodawać go do load. Odrzucone: net w żółtych godzinach znika, a właśnie ten eksport jest miarą tego, ile da się załadować.

## Consequences

Włączone stosowanie planu pokazuje rozkład w Prognozie i trzyma `ev_load_kwh` obok domu. Nie podnosi `load_kwh` ani pasma p75. Korekta mid-hour odejmuje energię już pobraną przez wallbox od licznika, zanim liczy k domu.
