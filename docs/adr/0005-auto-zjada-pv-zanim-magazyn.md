# Auto zjada PV, zanim magazyn

Dopisek do [0004](0004-auto-nie-wchodzi-do-load.md). `load_kwh` dalej jest zużyciem domu. `ev_load_kwh` wchodzi do bilansu MILP jako rezerwacja reszty PV, nie jako load.

Godzina z autem nie może jednocześnie oddać tej samej nadwyżki do magazynu. Plan z 2026-10-01 10:40 trzymał SOC na 17% do południa i ładował baterię w 12–14, w tych samych slotach co 8 kWh auta. Rano szło w eksport po 43–47 gr, a wieczorny zrzut po 1,15 zł nie miał skąd wziąć energii.

## Considered options

- Zostawić auto tylko w kolumnie EV, poza solverem. Odrzucone: solver rezerwuje południowe PV na magazyn i sprzedaje poranek.
- Dodać auto do `load_kwh`. Odrzucone, jak w 0004: slot większy niż PV robi import albo rozładowanie pod samochód.
- Obciąć slot do nadwyżki i dodać go do load. Odrzucone: to samo co rezerwacja z PV, tylko przez load, i gubi informację, gdy auto jest większe niż PV.

## Consequences

Z reszty PV schodzi `min(ev_load_kwh, pv_rem)`. Reszta idzie do domu i magazynu. Auto ponad PV nie podnosi importu ani rozładowania. Limit zielonego eksportu i `ch_pv` używają PV po tej rezerwacji. Scenariusz bierze własne PV, a slot auta zostaje wspólny.
