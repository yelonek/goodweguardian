# Nastawa w watach, dwa adaptery

Mózg bilansu wydaje nastawę w watach (moc baterii, limit SOC, oddanie sterowania). Guardian tłumaczy ją na slot Eco GoodWe. DeyeDefender bierze tę samą nastawę, ale nie zapisuje rejestrów, dopóki nie ma modelu i mapy. Procent slotu GoodWe (na tym domu 1% ≈ 70 W) na innym falowniku byłby inną mocą.

## Considered options

- Zostawienie decyzji w procentach EcoSlot i przeliczanie ich mocą znamionową Deye. Odrzucone, bo procent nie jest mocą.
- Osobny mózg dla Deye. Odrzucone, bo bilans, soak i obrony SOC są te same.
- Zgadywanie mapy Modbus Deye bez falownika pod ręką. Odrzucone, bo zły zapis steruje na ślepo.

## Consequences

Arytmetyka progów zostaje w procentach tego domu. Waty na nastawie to ten procent razy `watts_per_percent`, bez drugiego zaokrąglenia; adapter GoodWe dzieli z powrotem i dostaje ten sam procent slotu. Przepisywanie progów na waty w środku równań czeka na strojenie Deye. Ten dom i jego compose zostają przy Guardianie.
