"""Odczyt tylko wybranych sensorów (z sensor_mapping) z inwertera GoodWe."""
import asyncio
import os

import goodwe
from dotenv import load_dotenv

from sensor_mapping import (
    BATTERY_POWER,
    BATTERY_SOC,
    ENERGY_EXPORTED_DAY,
    ENERGY_EXPORTED_TOTAL,
    ENERGY_IMPORTED_DAY,
    ENERGY_IMPORTED_TOTAL,
    GRID_POWER,
    HOUSE_CONSUMPTION_POWER,
    PV_POWER,
)

load_dotenv()

SELECTED_IDS = frozenset({
    GRID_POWER,
    PV_POWER,
    BATTERY_POWER,
    HOUSE_CONSUMPTION_POWER,
    BATTERY_SOC,
    ENERGY_IMPORTED_TOTAL,
    ENERGY_EXPORTED_TOTAL,
    ENERGY_IMPORTED_DAY,
    ENERGY_EXPORTED_DAY,
})


async def main() -> None:
    ip = os.environ.get("INVERTER_IP")
    if not ip:
        raise SystemExit("Ustaw zmienną środowiskową INVERTER_IP")

    inverter = await goodwe.connect(ip)
    runtime_data = await inverter.read_runtime_data()
    sensors_by_id = {s.id_: s for s in inverter.sensors()}

    for sid in sorted(SELECTED_IDS):
        if sid not in runtime_data:
            print(f"{sid}: (brak)")
            continue
        s = sensors_by_id.get(sid)
        name = s.name if s else sid
        unit = f" {s.unit}" if s and s.unit else ""
        print(f"{sid}: {name} = {runtime_data[sid]}{unit}")


if __name__ == "__main__":
    asyncio.run(main())
