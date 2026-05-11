"""Mapowanie sensorów GoodWe na pojęcia biznesowe.

Dane z miernika (meter_*) używamy dla obciążeń po stronie sieci;
inwerter nie obsługuje całości instalacji. Dla każdej wielkości
wybieramy moc chwilową [W] tam gdzie to ma sens.

Konwencja znaku (cały guardian): DODATNIE = energia/moc w kierunku DO SIECI (eksport);
ujemne = z sieci (import / pobór).
"""

# Moc chwilowa [W] — z miernika (całość sieci)
GRID_POWER = "meter_active_power_total"

# Moc chwilowa [W] — z inwertera
PV_POWER = "ppv"
# Bateria: dodatnie = rozładowanie (moc na AC), ujemne = ładowanie — typowo zwiększa/zmniejsza eksport netto.
BATTERY_POWER = "pbattery1"
HOUSE_CONSUMPTION_POWER = "house_consumption"

# SOC [%] — z inwertera
BATTERY_SOC = "battery_soc"

# Energia skumulowana [kWh] — z miernika
ENERGY_IMPORTED_TOTAL = "meter_e_total_imp"   # zakupiona z sieci
ENERGY_EXPORTED_TOTAL = "meter_e_total_exp"  # oddana do sieci

# Energia PV skumulowana [kWh] — z inwertera (monotoniczny licznik produkcji PV)
PV_ENERGY_TOTAL = "e_total"

# Opcjonalnie: dzienne z inwertera (miernik może nie mieć dziennych)
ENERGY_IMPORTED_DAY = "e_day_imp"
ENERGY_EXPORTED_DAY = "e_day_exp"

POWER_SENSOR_IDS = frozenset({
    GRID_POWER,
    PV_POWER,
    BATTERY_POWER,
    HOUSE_CONSUMPTION_POWER,
})
