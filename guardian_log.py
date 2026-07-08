"""Logowanie interwencji i danych wejściowych – do debugowania."""
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from guardian_config import LOG_DIR, TELEMETRY_TZ


class _LocalTzFormatter(logging.Formatter):
    """%(asctime)s w TELEMETRY_TZ — zgodnie z dashboard | HH:MM:SS w logu."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=ZoneInfo(TELEMETRY_TZ))
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


def _log_path() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / "guardian.log"


def setup_logging() -> None:
    """Konfiguruje logging do pliku i konsoli."""
    path = _log_path()
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    formatter = _LocalTzFormatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def balancing_power_kw_signed(remaining_kwh: float, time_to_end_s: float) -> float:
    """Średnia moc „domykająca” bilans [kW], znak jak remaining_kWh (+ = strona eksportu)."""
    if time_to_end_s <= 0:
        return 0.0
    return remaining_kwh * 3600.0 / time_to_end_s


def log_dashboard(
    *,
    now: datetime,
    remaining_kwh: float,
    balancing_kw: float,
    grid_w: float,
    pv_w: float,
    consumption_w: float,
    soc_pct: float,
    battery_w: float,
    time_to_end_s: float,
    delta_imp_kwh: float,
    delta_exp_kwh: float,
    slot_active: bool,
    other_eco_active: bool = False,
    ecoslot_pct: int | None,
    intervene: bool,
    reason: str,
    threshold_kw: float,
    # Command (to co guardian chce ustawić TERAZ; różni się od ecoslot_pct, który jest readback)
    commanded_enabled: bool | None = None,
    commanded_pct: int | None = None,
    commanded_duration_s: float | None = None,
) -> None:
    """Jedna linia „pulpit”: balans godz., moc bilansowania, sieć/PV/dom, bateria, slot, próg."""
    log = logging.getLogger("guardian")
    pct = "—" if ecoslot_pct is None else str(ecoslot_pct)
    extra = f" | {reason}" if reason else ""
    if commanded_pct is not None and commanded_enabled is not None:
        extra += f" | cmd={'On' if commanded_enabled else 'Off'} {commanded_pct:+d}%"
        if commanded_duration_s is not None:
            extra += f" {commanded_duration_s:.0f}s"
    log.info(
        "dashboard | %s | balans_godz=%+.3f kWh (Δexp−Δimp; Δimp=%.3f Δexp=%.3f) | moc_bilans=%+.3f kW | "
        "sieć=%+.2f kW | PV=%.2f kW | dom=%.0f W | SOC=%.0f%% | P_bat=%+.0f W | "
        "do_końca=%.0fs | slot_bal=%s inny_eco=%s ecoslot_read%%=%s | próg=%.2f kW | interwen=%s%s",
        now.strftime("%H:%M:%S"),
        remaining_kwh,
        delta_imp_kwh,
        delta_exp_kwh,
        balancing_kw,
        grid_w / 1000.0,
        pv_w / 1000.0,
        consumption_w,
        soc_pct,
        battery_w,
        time_to_end_s,
        slot_active,
        other_eco_active,
        pct,
        threshold_kw,
        intervene,
        extra,
    )


def log_intervention(
    *,
    now: datetime,
    remaining_kwh: float,
    power_needed_kw: float,
    intervene: bool,
    battery_power_w: float | None = None,
    battery_power_pct: int | None = None,
    duration_s: float | None = None,
    reason: str = "",
) -> None:
    """Zapisuje informację o (nie)interwencji."""
    log = logging.getLogger("guardian")
    msg = (
        f"intervention | now={now.isoformat()} remaining_kWh={remaining_kwh:.4f} "
        f"power_needed_kW={power_needed_kw:.3f} intervene={intervene}"
    )
    if battery_power_w is not None:
        msg += f" battery_W={battery_power_w:.0f}"
    if battery_power_pct is not None:
        msg += f" battery_pct={battery_power_pct}"
    if duration_s is not None:
        msg += f" duration_s={duration_s:.0f}"
    if reason:
        msg += f" reason={reason}"
    log.debug(msg)


def log_inputs(
    *,
    now: datetime,
    E_exp: float,
    E_imp: float,
    E_exp_start: float | None,
    E_imp_start: float | None,
    pv_w: float,
    grid_w: float,
    consumption_w: float,
) -> None:
    """Zapisuje dane wejściowe (odczyty) dla debugowania."""
    log = logging.getLogger("guardian")
    log.debug(
        "inputs | %s E_exp=%.4f E_imp=%.4f start=(%s,%s) pv_w=%.0f grid_w=%.0f consumption_w=%.0f",
        now.isoformat(),
        E_exp,
        E_imp,
        f"{E_exp_start:.4f}" if E_exp_start is not None else "None",
        f"{E_imp_start:.4f}" if E_imp_start is not None else "None",
        pv_w,
        grid_w,
        consumption_w,
    )


def log_ecoslot_failure(slot_id: str, error: Exception) -> None:
    """Zapisuje niepowodzenie ustawienia ecoslota (1 próba, bez retry)."""
    log = logging.getLogger("guardian")
    log.error("ecoslot write failed | slot=%s error=%s", slot_id, error)
