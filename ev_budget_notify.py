"""Powiadomienia budżetu ładowania: raz o 8:00 i przy przekroczeniu licznika od 8:00."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from anomaly_alerts import charging_kw_from_twc
from ev_day_budget import DayChargingBudget, charging_page_view
from guardian_config import EV_BUDGET_NOTICE_PATH, TELEMETRY_TZ
from ntfy_notify import send_ntfy
from pv_pyramid import BUDGET_FROM_HOUR

log = logging.getLogger("guardian")

CHARGING_ON_KW = 0.2
OVER_EPS_KWH = 0.05

SendFn = Callable[..., bool]


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=ZoneInfo(TELEMETRY_TZ))
    return now.astimezone(ZoneInfo(TELEMETRY_TZ))


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        log.warning("ev budget notice state read failed: %s", e)
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _window_text(budget: DayChargingBudget) -> str:
    if budget.window_start_hour is None or budget.window_end_hour is None:
        return "brak okna"
    return f"{budget.window_start_hour:02d}:00–{budget.window_end_hour:02d}:00"


def _morning_body(budget: DayChargingBudget) -> str:
    return (
        f"Budżet {budget.budget_kwh:.1f} kWh "
        f"({budget.coefficient:.0%} p50, próg {budget.threshold_pln:.2f} zł). "
        f"Okno {_window_text(budget)}. Limit ustaw w aucie."
    )


def _over_body(delivered_kwh: float, budget: DayChargingBudget) -> str:
    return (
        f"Weszło {delivered_kwh:.1f} kWh od {BUDGET_FROM_HOUR}:00, "
        f"budżet jest {budget.budget_kwh:.1f} kWh. Zejdź z limitem w aplikacji auta."
    )


def process_ev_budget_notices(
    *,
    now: datetime,
    delivered_since_8_kwh: float,
    budget: DayChargingBudget,
    charging_kw: float | None,
    path: Path | None = None,
    send: SendFn = send_ntfy,
) -> list[str]:
    """Zwraca rodzaje wysłanych komunikatów: ``morning``, ``over_budget``."""
    aware = _aware(now)
    target = path or EV_BUDGET_NOTICE_PATH
    state = _read_state(target)
    sent: list[str] = []
    today = aware.date().isoformat()

    if aware.hour >= BUDGET_FROM_HOUR and state.get("morning_sent_date") != today:
        ok = send("Ładowanie: budżet na dziś", _morning_body(budget))
        if ok:
            state["morning_sent_date"] = today
            sent.append("morning")

    over = float(delivered_since_8_kwh) > float(budget.budget_kwh) + OVER_EPS_KWH
    charging = charging_kw is not None and float(charging_kw) > CHARGING_ON_KW
    if not over:
        state["over_budget_kwh"] = None
    elif charging:
        last = state.get("over_budget_kwh")
        last_f = float(last) if last is not None else None
        budget_dropped = last_f is None or float(budget.budget_kwh) < last_f - OVER_EPS_KWH
        if budget_dropped:
            ok = send("Ładowanie: powyżej budżetu", _over_body(delivered_since_8_kwh, budget))
            if ok:
                state["over_budget_kwh"] = budget.budget_kwh
                sent.append("over_budget")

    _write_state(target, state)
    return sent


def notice_cycle(
    *,
    now: datetime,
    E_twc_kwh: float | None,
    path: Path | None = None,
    send: SendFn = send_ntfy,
) -> list[str]:
    """Jeden cykl pętli. Nie rzuca przy błędzie prognozy."""
    aware = _aware(now)
    target = path or EV_BUDGET_NOTICE_PATH
    state = _read_state(target)
    prev_kwh = state.get("prev_e_twc_kwh")
    prev_ts = state.get("prev_ts")
    dt_s = 0.0
    if isinstance(prev_ts, str):
        try:
            prev = datetime.fromisoformat(prev_ts)
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=ZoneInfo(TELEMETRY_TZ))
            dt_s = (aware - prev).total_seconds()
        except ValueError:
            dt_s = 0.0
    charging_kw = charging_kw_from_twc(
        float(prev_kwh) if prev_kwh is not None else None,
        E_twc_kwh,
        dt_s,
    )
    state["prev_e_twc_kwh"] = E_twc_kwh
    state["prev_ts"] = aware.isoformat()
    _write_state(target, state)
    try:
        view = charging_page_view(aware)
    except Exception as e:
        log.warning("ev budget view failed: %s", e)
        return []
    budget = DayChargingBudget(
        surplus_kwh=float(view["surplus_kwh"]),
        budget_kwh=float(view["budget_kwh"]),
        coefficient=float(view["coefficient"]),
        window_start_hour=view["window_start_hour"],
        window_end_hour=view["window_end_hour"],
        threshold_pln=float(view["threshold_pln"]),
    )
    return process_ev_budget_notices(
        now=aware,
        delivered_since_8_kwh=float(view["delivered_since_8_kwh"]),
        budget=budget,
        charging_kw=charging_kw,
        path=target,
        send=send,
    )
