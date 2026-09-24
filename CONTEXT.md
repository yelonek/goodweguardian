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

**Reszta godziny**:
Planowany bilans godzinowy minus bieżący bilans. Ujemna reszta przy dodatnim bilansie godzinowym to ściągnięcie już wyeksportowanych kWh, nie zakup z sieci.
_Avoid_: import, odkup

**Soak**:
Schowanie energii w magazynie przez zmniejszenie netu godziny. Dopóki bilans godzinowy zostaje dodatni, utracone korzyści to RCE tej godziny (po ucięciu ujemnej ceny do 0).
_Avoid_: odkup po G12, clawback jako kupno po taryfie importu
