"""Debounce / cooldown / persist aktywnych alarmów + push ntfy."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from anomaly_alerts import AlertFiring, AnomalySnapshot, charging_kw_from_twc, evaluate
from guardian_config import ANOMALY_ALERTS_PATH, TELEMETRY_TZ
from guardian_settings import get_settings
from ntfy_notify import ntfy_configured, send_ntfy

log = logging.getLogger("guardian")


class RuleRuntime(BaseModel):
    consecutive_hit: int = 0
    consecutive_miss: int = 0
    last_fired_at: str | None = None
    active: bool = False
    activated_at: str | None = None
    title: str = ""
    body: str = ""
    est_pln_per_h: float = 0.0


class AlertFileState(BaseModel):
    prev_e_twc_kwh: float | None = None
    prev_soc_pct: float | None = None
    prev_ts: str | None = None
    rules: dict[str, RuleRuntime] = Field(default_factory=dict)


def _path() -> Path:
    return ANOMALY_ALERTS_PATH


def _read_state(path: Path | None = None) -> AlertFileState:
    p = path or _path()
    if not p.exists():
        return AlertFileState()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("anomaly_alerts.json read failed: %s", e)
        return AlertFileState()
    if not isinstance(data, dict):
        return AlertFileState()
    try:
        return AlertFileState.model_validate(data)
    except Exception as e:
        log.warning("anomaly_alerts.json invalid: %s", e)
        return AlertFileState()


def _write_state(state: AlertFileState, path: Path | None = None) -> None:
    p = path or _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    payload = json.dumps(state.model_dump(), indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(p)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(TELEMETRY_TZ))
    return dt


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return _aware(dt)


def _iso(now: datetime) -> str:
    return _aware(now).isoformat(timespec="seconds")


def apply_firings(
    state: AlertFileState,
    firings: list[AlertFiring],
    *,
    now: datetime,
    debounce_min: int,
    cooldown_min: int,
    clear_min: int,
) -> list[AlertFiring]:
    """Aktualizuje ``state.rules``. Zwraca firingi do pusha (po debounce+cooldown)."""
    now_iso = _iso(now)
    by_id = {f.rule_id: f for f in firings}
    known = set(state.rules) | set(by_id)
    to_push: list[AlertFiring] = []

    for rule_id in known:
        rt = state.rules.get(rule_id) or RuleRuntime()
        firing = by_id.get(rule_id)
        if firing is not None:
            rt.consecutive_hit += 1
            rt.consecutive_miss = 0
            rt.title = firing.title
            rt.body = firing.body
            rt.est_pln_per_h = firing.est_pln_per_h
            ready = rt.consecutive_hit >= debounce_min
            last = _parse_iso(rt.last_fired_at)
            cooled = last is None or (_aware(now) - last) >= timedelta(minutes=cooldown_min)
            if ready and cooled:
                rt.active = True
                if not rt.activated_at:
                    rt.activated_at = now_iso
                rt.last_fired_at = now_iso
                to_push.append(firing)
            elif ready:
                rt.active = True
                if not rt.activated_at:
                    rt.activated_at = now_iso
        else:
            rt.consecutive_hit = 0
            rt.consecutive_miss += 1
            if rt.consecutive_miss >= clear_min:
                rt.active = False
                rt.activated_at = None
                rt.title = ""
                rt.body = ""
                rt.est_pln_per_h = 0.0
        state.rules[rule_id] = rt
    return to_push


def alerts_api_payload(path: Path | None = None) -> dict[str, Any]:
    state = _read_state(path)
    active: list[dict[str, Any]] = []
    for rule_id, rt in state.rules.items():
        if not rt.active:
            continue
        active.append(
            {
                "rule_id": rule_id,
                "title": rt.title,
                "body": rt.body,
                "est_pln_per_h": rt.est_pln_per_h,
                "since": rt.activated_at,
            }
        )
    active.sort(key=lambda x: x["rule_id"])
    return {"active": active, "ntfy_configured": ntfy_configured()}


def process_cycle_alerts(
    *,
    now: datetime,
    grid_w: float,
    pv_w: float,
    consumption_w: float,
    soc_pct: float,
    battery_w: float,
    remaining_kwh: float,
    time_to_end_s: float,
    plan_target_net_kwh: float | None,
    exec_mode: str | None,
    planner_execution_enabled: bool,
    policy_missing: bool,
    control_enabled: bool,
    watchdog_reason: str,
    E_twc_kwh: float | None,
    path: Path | None = None,
    notify: bool = True,
) -> list[AlertFiring]:
    """Jeden cykl: TWC kW, evaluate, debounce, opcjonalny ntfy. Nie rzuca na zewnątrz."""
    s = get_settings()
    if not s.anomaly_alerts_enabled:
        return []
    p = path or _path()
    state = _read_state(p)
    aware = now if now.tzinfo is not None else now.replace(tzinfo=ZoneInfo(TELEMETRY_TZ))
    prev_ts = _parse_iso(state.prev_ts)
    dt_s = 0.0
    if prev_ts is not None:
        dt_s = (aware - prev_ts).total_seconds()
    charging_kw = charging_kw_from_twc(state.prev_e_twc_kwh, E_twc_kwh, dt_s)

    snap = AnomalySnapshot(
        local_hour=aware.hour,
        local_minute=aware.minute,
        grid_w=grid_w,
        pv_w=pv_w,
        consumption_w=consumption_w,
        soc_pct=soc_pct,
        battery_w=battery_w,
        remaining_kwh=remaining_kwh,
        time_to_end_s=time_to_end_s,
        plan_target_net_kwh=plan_target_net_kwh,
        exec_mode=exec_mode,
        planner_execution_enabled=planner_execution_enabled,
        policy_missing=policy_missing,
        control_enabled=control_enabled,
        watchdog_reason=watchdog_reason,
        charging_kw=charging_kw,
    )
    firings = evaluate(snap)
    to_push = apply_firings(
        state,
        firings,
        now=aware,
        debounce_min=int(s.anomaly_debounce_min),
        cooldown_min=int(s.anomaly_cooldown_min),
        clear_min=int(s.anomaly_clear_min),
    )
    state.prev_e_twc_kwh = E_twc_kwh
    state.prev_soc_pct = soc_pct
    state.prev_ts = _iso(aware)
    _write_state(state, p)

    if notify:
        for firing in to_push:
            send_ntfy(firing.title, firing.body)
    return to_push
