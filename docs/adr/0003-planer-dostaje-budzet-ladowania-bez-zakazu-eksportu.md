# Planer dostaje budżet ładowania, bez zakazu eksportu

Ładowanie dzienne bierze tylko tanią nadwyżkę (RCE poniżej 59 gr/kWh). Przy włączonym stosowaniu planu ładowania Planer dostaje aktualny budżet jako zużycie auta i nic ponadto. Nie zabrania sprzedaży tej nadwyżki, nawet gdy rano RCE jest trochę wyższe niż po południu.

## Considered options

- Wpisanie całej taniej nadwyżki jako zużycia auta w drogiej porannej godzinie. Odrzucone, bo Planer uznaje energię za zjedzoną i nie chowa jej w magazynie, a auto i tak bierze tylko 6 A. Reszta wychodzi na licznik i po zamknięciu godziny jest sprzedana.
- Limit eksportu w godzinie poniżej 59 gr: nadwyżkę powyżej 6 A ładować do magazynu pod późniejsze auto. Odrzucone. Działa, ale to osobna reguła obok cashflow. Korektę użytkownik robi sam: współczynnik, limit w aucie albo wyłączenie przełącznika.

## Consequences

Przełącznik startuje wyłączony i zostaje, dopóki użytkownik go nie zmieni. Wyłączony: Planer o aucie nie wie. Włączony: przy każdym przeliczeniu dostaje żywy budżet, także gdy auto nie jest podpięte. Ładowanie nocne przed wyjazdem do tego nie wchodzi. Limit i prąd ładowania zostają w aucie.
