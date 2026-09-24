# Rachunek sprzedawcy to jeden bilans godzinowy

Rachunek sprzedawcy energii za godzinę liczy się w jednym miejscu: dodatni bilans godzinowy cały po `max(RCE, 0)`, ujemny cały po G12. W otwartej godzinie to miejsce wycenia planowany bilans godzinowy i dokłada bieżący bilans oraz resztę godziny. Po zamknięciu ta sama cena idzie od netu, który naprawdę wyszedł. Guardian, mapper `exec_mode` i KPI biorą cenę stąd i nie odejmują licznika od planu drugi raz. Inaczej soak przy godzinie, która zostaje na plusie, wraca jako „odkup po G12”, a ujemne RCE rozjeżdża plan i KPI.

## Considered options

- Dwa rachunki w jednej godzinie: chwilowy minus po G12 i późniejszy eksport po RCE. Odrzucone, bo sprzedawca widzi tylko bilans zamknięcia.
- Osobne mnożenie RCE w KPI i w Guardianie. Odrzucone, bo ujemne RCE prosumenta ma wejść jako 0 w obu.
- Zostawienie odejmowania `N₀` w `hour_remainder`, `hour_plan_export`, `policy_output` i we Flappy. Odrzucone, bo każdy wywołujący zgaduje cenę reszty sam.

## Consequences

MILP co 10 minut tylko wybiera planowany bilans godzinowy. Zużycie baterii, korekty PV i load oraz limit 0,5 kWh w egzekucji zostają poza tym miejscem. Test zdania o rachunku to trzy przypadki: licznik +0,97 przy planie +0,23 (utracone korzyści to RCE), licznik −0,10 przy planie +0,23 (nadal +0,23 × RCE), plan ujemny (cały niedobór po G12, nawet gdy licznik jest jeszcze na plusie).
