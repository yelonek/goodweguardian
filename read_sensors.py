"""Odczyt danych z sensorów inwertera GoodWe (IP z env INVERTER_IP)."""
import asyncio
import os

import goodwe
from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    ip = os.environ.get("INVERTER_IP")
    if not ip:
        raise SystemExit("Ustaw zmienną środowiskową INVERTER_IP")

    inverter = await goodwe.connect(ip)
    runtime_data = await inverter.read_runtime_data()

    for sensor in inverter.sensors():
        if sensor.id_ in runtime_data:
            val = runtime_data[sensor.id_]
            unit = f" {sensor.unit}" if sensor.unit else ""
            print(f"{sensor.id_}: {sensor.name} = {val}{unit}")


if __name__ == "__main__":
    asyncio.run(main())
