# GoodWe Guardian

Dom z fotowoltaiką i magazynem, rozliczany jako prosument godzina po godzinie.

## Language

### Bilans

**Bilansowanie godzinowe**:
Jedna godzina zegarowa ma jeden net. Eksport i import w tej samej godzinie nie są dwoma rachunkami.
_Avoid_: osobna sprzedaż i osobny zakup w tej samej godzinie

**Bieżący bilans**:
Net na liczniku otwartej godziny, teraz.
_Avoid_: bilans godzinowy, N₀, chwilowa moc

**Bilans godzinowy**:
Net zamknięcia godziny, który wchodzi na rachunek sprzedawcy energii. W otwartej godzinie liczba z Planera to planowany bilans godzinowy; po zamknięciu ten sam termin oznacza net, który naprawdę się zamknął.
_Avoid_: bieżący bilans, chwilowa moc, osobny rachunek za chwilowy minus

**Net**:
Energia wyeksportowana minus energia zaimportowana na liczniku w jednej godzinie zegarowej. Net > 0 sprzedaje się cały po RCE. Net < 0 kupuje się cały po G12.
_Avoid_: chwilowa moc na liczniku, dwa osobne rozliczenia importu i eksportu

**RCE**:
Cena sprzedaży nadwyżki, gdy bilans godzinowy > 0. Dla prosumenta ujemne RCE nie istnieje: do rachunku wchodzi 0.
_Avoid_: stawka importu, G12, ujemna cena sprzedaży

**G12**:
Cena zakupu niedoboru zamkniętej godziny, gdy net < 0. Wchodzi dopiero wtedy, gdy zamknięta godzina ma net < 0.
_Avoid_: koszt cofnięcia eksportu w godzinie, która zostaje netto eksportem

**Depozyt**:
Wpływ za godzinę z net > 0: nadwyżka razy RCE, ucięte od dołu do 0.
_Avoid_: cashflow, rachunek

**Cashflow**:
Depozyt minus rachunek za godziny z importem. Ma pierwszeństwo przed ładowaniem dziennym.
_Avoid_: depozyt

**Reszta godziny**:
Planowany bilans godzinowy minus bieżący bilans. Ujemna reszta przy dodatnim bilansie godzinowym to ściągnięcie już wyeksportowanych kWh, nie zakup z sieci.
_Avoid_: import, odkup

**Soak**:
Schowanie energii w magazynie przez zmniejszenie netu godziny. Dopóki bilans godzinowy zostaje dodatni, utracone korzyści to RCE tej godziny (po ucięciu ujemnej ceny do 0).
_Avoid_: odkup po G12, clawback jako kupno po taryfie importu

### Sterowanie

**Planer**:
Ustala politykę godziny. Nie zapisuje na falownik.
_Avoid_: Guardian, DeyeDefender, nastawa

**Stosowanie planu**:
Czy Guardian używa bieżącego planu przy sterowaniu. Plan liczy się dalej, także gdy stosowanie jest wyłączone. Zapis na falownik to osobny przełącznik.
_Avoid_: akceptacja planu, zatrzymanie planera, zapis na falownik

**Nastawa**:
Moc baterii w watach, limit SOC i czy oddać falownik jego własnemu programowi.
_Avoid_: procent slotu, EcoSlot, power_pct

**Guardian**:
Adapter, który nastawę zapisuje na falowniku GoodWe.
_Avoid_: cały program, planer, DeyeDefender

**DeyeDefender**:
Adapter, który tę samą nastawę zapisuje na falowniku Deye.
_Avoid_: drugi planer, Guardian

### Samochód

**Ładowanie wyjazdowe**:
Ładowanie auta w nocy poprzedzającej wyjazd.
_Avoid_: plan, zlecenie, ładowanie dzienne

**Ładowanie dzienne**:
Ładowanie auta tanią nadwyżką. CWU do tego nie należy.
_Avoid_: CWU, bojler, ładowanie wyjazdowe, soak, plan

**Tania nadwyżka**:
Nadwyżka PV ponad zużycie domu w godzinie, której RCE jest niższe niż cena taniej taryfy. Próg w tym domu to 59 gr/kWh. Powyżej progu kWh idzie w depozyt.
_Avoid_: godzina nocnej strefy G12, cała produkcja PV, depozyt

**Propozycja**:
Budżet tego dnia pokazany na dwa sposoby naraz: procent taniej nadwyżki i kWh. Do tego okno godzin.
_Avoid_: zlecenie, p10, komunikat wieczorny na jutro, instrukcja amperów, CWU

**Rozkład**:
Godziny ładowania dziennego od startu, w każdej najwyżej moc. Tylko godziny z energią tańszą niż tania taryfa, bez nocy 22–6. Reszta przy każdym przeliczeniu to budżet minus energia już pobrana z wallboxa od 8:00.
_Avoid_: plan, ręczna siatka godzin, ładowanie wyjazdowe

**Start**:
Pierwsza godzina rozkładu. Samo: pierwsza pozostała godzina taniej nadwyżki. Da się przesunąć ręcznie.
_Avoid_: godzina ustawiona w aucie, 22:00

**Moc ładowania**:
Najwięcej kWh, jakie rozkład kładzie w jednej godzinie. Samo: 3 kW. Da się zmienić ręcznie.
_Avoid_: prąd w amperach w aucie, moc falownika

**Stosowanie planu ładowania**:
Trwały przełącznik w Ustawieniach, obok sterowania Planerem i Guardianem. Na starcie wyłączony. Włączony: przy każdym przeliczeniu Planer dostaje aktualny budżet jako zużycie ładowania dziennego, także gdy auto nie jest podpięte, aż go wyłączysz.
_Avoid_: stosowanie planu, rezerwa w magazynie, limit eksportu, przekroczenie

**Budżet**:
Współczynnik propozycji razy aktualne p50 taniej nadwyżki od 8:00 do końca dnia. Bez przypięcia rusza się z prognozą.
_Avoid_: p10, licznik od północy, ładowanie wyjazdowe, przypięcie

**Przypięcie**:
Ręcznie ustawiona suma kWh, start albo moc. Trzyma do najbliższego 8:00 albo do powrotu na automat. Gdy prognoza taniej nadwyżki spadnie poniżej trzymanej sumy, suma schodzi do tej nadwyżki, więc procent nie przekracza 100%. Rozkład i tak skraca się o energię już pobraną z wallboxa.
_Avoid_: drugi budżet, ręczna siatka godzin, procent powyżej 100%

**Przekroczenie**:
Energia na liczniku ładowarki od 8:00 powyżej budżetu. Drugi komunikat podaje, ile weszło i jaki jest budżet. Limit obniżasz w aucie. Przy odpiętym aucie komunikatu nie ma.
_Avoid_: spadek prognozy bez ładowania, ładowanie nocne

**Współczynnik propozycji**:
Ułamek p50 taniej nadwyżki podawany do auta. Co rano, bez przypięcia, stoi na 100%. Da się go zmienić.
_Avoid_: p10

**Autokonsumpcja**:
Zużycie produkcji PV w domu albo w aucie w tej samej godzinie, kosztem sprzedaży po RCE tej godziny.
_Avoid_: depozyt, uniknięcie G12, darmowa energia
