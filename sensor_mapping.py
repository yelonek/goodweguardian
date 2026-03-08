"""Mapowanie sensorów GoodWe na pojęcia biznesowe.

Dane z miernika (meter_*) używamy dla obciążeń po stronie sieci;
inwerter nie obsługuje całości instalacji. Dla każdej wielkości
wybieramy moc chwilową [W] tam gdzie to ma sens.
"""

# Moc chwilowa [W] — z miernika (całość sieci)
# Znak: ujemne = pobór z sieci (import), dodatnie = wysyłanie do sieci (eksport)
GRID_POWER = "meter_active_power_total"

# Moc chwilowa [W] — z inwertera
PV_POWER = "ppv"
# Znak: ujemne = ładowanie baterii, dodatnie = rozładowanie
BATTERY_POWER = "pbattery1"
HOUSE_CONSUMPTION_POWER = "house_consumption"

# SOC [%] — z inwertera
BATTERY_SOC = "battery_soc"

# Energia skumulowana [kWh] — z miernika
ENERGY_IMPORTED_TOTAL = "meter_e_total_imp"   # zakupiona z sieci
ENERGY_EXPORTED_TOTAL = "meter_e_total_exp"  # oddana do sieci

# Opcjonalnie: dzienne z inwertera (miernik może nie mieć dziennych)
ENERGY_IMPORTED_DAY = "e_day_imp"
ENERGY_EXPORTED_DAY = "e_day_exp"

POWER_SENSOR_IDS = frozenset({
    GRID_POWER,
    PV_POWER,
    BATTERY_POWER,
    HOUSE_CONSUMPTION_POWER,
})
