"""Odczyt ecoslotów (eco_mode_1..4) z inwertera GoodWe.

Ecosloty to ustawienia (settings), nie runtime sensors.
Dostępne w modelach ET/ES; DT nie ma eco_mode_*.
"""
import asyncio
import os

import goodwe
from dotenv import load_dotenv

load_dotenv()

ECO_SETTING_IDS = ("eco_mode_1", "eco_mode_2", "eco_mode_3", "eco_mode_4")


async def main() -> None:
    ip = os.environ.get("INVERTER_IP")
    if not ip:
        raise SystemExit("Ustaw zmienną środowiskową INVERTER_IP")

    inverter = await goodwe.connect(ip)
    settings_names = {s.id_ for s in inverter.settings()}

    for sid in ECO_SETTING_IDS:
        if sid not in settings_names:
            print(f"{sid}: (nieobsługiwane na tym modelu)")
            continue
        try:
            slot = await inverter.read_setting(sid)
        except Exception as e:
            print(f"{sid}: błąd odczytu: {e}")
            continue
        if slot is None:
            print(f"{sid}: (brak danych)")
            continue
        # EcoModeV1 / EcoModeV2 / Schedule: start_h, start_m, end_h, end_m, power, days, on_off
        start = f"{getattr(slot, 'start_h', 0):02d}:{getattr(slot, 'start_m', 0):02d}"
        end = f"{getattr(slot, 'end_h', 0):02d}:{getattr(slot, 'end_m', 0):02d}"
        power = getattr(slot, 'power', None)
        days = getattr(slot, 'days', "")
        soc = getattr(slot, 'soc', None)
        on_off = getattr(slot, 'on_off', 0)
        active = "On" if on_off != 0 else "Off"
        power_unit = getattr(slot, 'get_power_unit', lambda: "%")()
        power_val = getattr(slot, 'get_power', lambda: power)() if power is not None else power
        soc_s = f" SoC {soc}%" if soc is not None else ""
        print(f"{sid}: {start}-{end} {days}{soc_s} | {power_val}{power_unit} | {active}")


if __name__ == "__main__":
    asyncio.run(main())
