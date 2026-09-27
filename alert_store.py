"""Debounce, zbiorczy push ntfy i odczyt planu / RCE / TWC dla alarmów."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from anomaly_alerts import (
    CURRENT_RULES,
    AlertFiring,
    AlertOutlook,
    AnomalySnapshot,
    PlanSlot,
    charging_kw_from_twc,
    day_is_armed,
    evaluate,
)
from energy_pricing import PRICING_CACHE_DIR
from ev_charging_store import read_declaration
from guardian_config import ANOMALY_ALERTS_PATH, TELEMETRY_TZ
from guardian_settings import get_settings
from ntfy_notify import ntfy_configured, send_ntfy
from planner.plan_store import load_latest_plan
from tariff_g12 import g12_tariff_from_env
from tesla_wall_charger import hourly_ev_kwh_from_telemetry

log = logging.getLogger("guardian")

PACE_KEEP_S = 10 * 60.0


class PaceSample(BaseModel):
    ts: str
    remaining_kwh: float


class RuleRuntime(BaseModel):
    consecutive_hit: int = 0
    consecutive_miss: int = 0
    last_fired_at: str | None = None
    active: bool = False
    activated_at: str | None = None
    title: str = ""
    body: str = ""
    est_pln_per_h: float = 0.0
    remind: bool = True
    once_per_hour: bool = False
    pushed_hour: str | None = None


class AlertFileState(BaseModel):
    prev_e_twc_kwh: float | None = None
    prev_soc_pct: float | None = None
    prev_ts: str | None = None
    last_digest_at: str | None = None
    digest_rule_ids: list[str] = Field(default_factory=list)
    pace: list[PaceSample] = Field(default_factory=list)
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


def _hour_key(now: datetime) -> str:
    aware = _aware(now)
    return f"{aware.date().isoformat()}T{aware.hour:02d}"


def _remember_pace(state: AlertFileState, now: datetime, remaining_kwh: float) -> None:
    aware = _aware(now)
    state.pace.append(PaceSample(ts=_iso(aware), remaining_kwh=float(remaining_kwh)))
    kept: list[PaceSample] = []
    for sample in state.pace:
        ts = _parse_iso(sample.ts)
        if ts is None:
            continue
        if (aware - ts).total_seconds() <= PACE_KEEP_S:
            kept.append(sample)
    state.pace = kept


def _pace_window(
    state: AlertFileState, now: datetime
) -> tuple[float | None, float | None]:
    """Bilans sprzed ~5 min i odstęp w sekundach. Brak pary → (None, None)."""
    aware = _aware(now)
    best: tuple[float, float, float] | None = None
    for sample in state.pace:
        ts = _parse_iso(sample.ts)
        if ts is None:
            continue
        dt_s = (aware - ts).total_seconds()
        if dt_s < 4.5 * 60 or dt_s > 6.5 * 60:
            continue
        dist = abs(dt_s - 5 * 60)
        if best is None or dist < best[0]:
            best = (dist, dt_s, sample.remaining_kwh)
    if best is None:
        return None, None
    return best[2], best[1]


def _rce_from_disk(local_date: date) -> list[float] | None:
    path = PRICING_CACHE_DIR / f"proxy_rce_{local_date.isoformat()}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("RCE cache read failed %s: %s", path, e)
        return None
    hourly = raw.get("hourly_rce_pln_per_kwh") if isinstance(raw, dict) else None
    if not isinstance(hourly, list) or len(hourly) != 24:
        return None
    try:
        return [float(v) for v in hourly]
    except (TypeError, ValueError):
        return None


def _planned_ev_kwh(plan_blob: dict[str, Any], day: str) -> dict[int, float]:
    """Sloty z plan_latest. Pusty słownik, gdy nie ma deklaracji — stary plan nie uzbraja."""
    if read_declaration() is None:
        return {}
    ev_root = plan_blob.get("ev_charging_plans")
    if not isinstance(ev_root, dict):
        return {}
    day_plan = ev_root.get(day)
    if not isinstance(day_plan, dict):
        return {}
    out: dict[int, float] = {}
    for key in ("slots", "past_slots"):
        rows = day_plan.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or str(row.get("date")) != day:
                continue
            try:
                hour = int(row["hour"])
                kwh = float(row.get("kwh") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            out[hour] = out.get(hour, 0.0) + max(0.0, kwh)
    return out


def load_alert_outlook(
    now: datetime,
    *,
    pace_remaining_kwh: float | None,
    pace_dt_s: float | None,
) -> AlertOutlook:
    """Plan, RCE z dysku i TWC. Bez sieci. Błąd jednego źródła nie gasi reszty."""
    aware = _aware(now)
    today = aware.date()
    day = today.isoformat()
    tariff = g12_tariff_from_env()
    settings = get_settings()
    rce = _rce_from_disk(today)
    rce_now = rce[aware.hour] if rce is not None else None

    actual: dict[int, float] = {}
    try:
        actual = hourly_ev_kwh_from_telemetry(today)
    except Exception as e:
        log.warning("EV telemetry for alerts failed: %s", e)

    planned: dict[int, float] = {}
    plan_hours: list[PlanSlot] = []
    try:
        plan = load_latest_plan()
    except Exception as e:
        log.warning("plan_latest for alerts failed: %s", e)
        plan = None
    if plan is not None:
        snap = plan.inputs_snapshot if isinstance(plan.inputs_snapshot, dict) else {}
        planned = _planned_ev_kwh(snap, day)
        by_hour = {
            int(hp.hour): hp
            for hp in plan.hours
            if hp.date == day and int(hp.hour) >= aware.hour
        }
        hours = set(by_hour) | {h for h, kwh in planned.items() if h >= aware.hour}
        for hour in sorted(hours):
            hp = by_hour.get(hour)
            plan_hours.append(
                PlanSlot(
                    hour=hour,
                    target_net_kwh=None if hp is None else float(hp.target_net_kwh),
                    ev_kwh=float(planned.get(hour, 0.0)),
                    rce_pln_kwh=None if rce is None else rce[hour],
                )
            )

    return AlertOutlook(
        rce_pln_kwh=rce_now,
        cheap_import_pln_kwh=tariff.import_pln_per_kwh(0),
        soc_full_pct=float(settings.soc_full_defense_threshold_pct),
        day_armed=day_is_armed(actual, planned),
        pace_remaining_kwh=pace_remaining_kwh,
        pace_dt_s=pace_dt_s,
        plan_hours=plan_hours,
    )


def _digest(rules: list[RuleRuntime]) -> AlertFiring:
    if len(rules) == 1:
        rt = rules[0]
        return AlertFiring(rule_id="digest", title=rt.title, body=rt.body)
    parts = [f"{rt.title}\n{rt.body}".strip() for rt in rules]
    n = len(rules)
    noun = "alarmy" if n < 5 else "alarmów"
    return AlertFiring(
        rule_id="digest",
        title=f"Guardian: {n} {noun}",
        body="\n\n".join(parts),
    )


def apply_firings(
    state: AlertFileState,
    firings: list[AlertFiring],
    *,
    now: datetime,
    debounce_min: int,
    cooldown_min: int,
    clear_min: int,
) -> list[AlertFiring]:
    """Aktualizuje reguły. Zwraca co najwyżej jeden zbiorczy push."""
    now_iso = _iso(now)
    hour_key = _hour_key(now)
    by_id = {f.rule_id: f for f in firings}
    known = set(state.rules) | set(by_id)

    for rule_id in known:
        rt = state.rules.get(rule_id) or RuleRuntime()
        firing = by_id.get(rule_id)
        if firing is not None:
            rt.consecutive_hit += 1
            rt.consecutive_miss = 0
            rt.title = firing.title
            rt.body = firing.body
            rt.est_pln_per_h = firing.est_pln_per_h
            rt.remind = firing.remind
            rt.once_per_hour = firing.once_per_hour
            need = 1 if firing.immediate else int(debounce_min)
            if rt.consecutive_hit >= need:
                rt.active = True
                if not rt.activated_at:
                    rt.activated_at = now_iso
        else:
            rt.consecutive_hit = 0
            rt.consecutive_miss += 1
            if rule_id not in CURRENT_RULES:
                rt.consecutive_miss = max(rt.consecutive_miss, clear_min)
            if rt.consecutive_miss >= clear_min:
                rt.active = False
                rt.activated_at = None
                rt.title = ""
                rt.body = ""
                rt.est_pln_per_h = 0.0
                rt.remind = True
                rt.once_per_hour = False
        state.rules[rule_id] = rt

    announced: list[tuple[str, RuleRuntime]] = []
    for rule_id, rt in state.rules.items():
        if not rt.active:
            continue
        if rt.once_per_hour and rt.pushed_hour == hour_key:
            continue
        announced.append((rule_id, rt))

    announced_ids = {rule_id for rule_id, _rt in announced}
    new_ids = announced_ids - set(state.digest_rule_ids)
    last = _parse_iso(state.last_digest_at)
    cooled = last is None or (_aware(now) - last) >= timedelta(minutes=int(cooldown_min))
    remind_due = cooled and any(rt.remind for _rule_id, rt in announced)
    if not announced or (not new_ids and not remind_due):
        state.digest_rule_ids = [
            rule_id for rule_id in state.digest_rule_ids if rule_id in announced_ids
        ]
        return []

    for rule_id, rt in announced:
        rt.last_fired_at = now_iso
        if rt.once_per_hour:
            rt.pushed_hour = hour_key
        state.rules[rule_id] = rt
    state.digest_rule_ids = sorted(announced_ids)
    state.last_digest_at = now_iso
    ordered = [rt for _rule_id, rt in sorted(announced, key=lambda item: item[0])]
    return [_digest(ordered)]


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
    outlook: AlertOutlook | None = None,
) -> list[AlertFiring]:
    """Jeden cykl: evaluate, debounce, jeden ewentualny ntfy. Nie rzuca na zewnątrz."""
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
    pace_remaining, pace_dt = _pace_window(state, aware)

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
    view = outlook
    if view is None:
        try:
            view = load_alert_outlook(
                aware, pace_remaining_kwh=pace_remaining, pace_dt_s=pace_dt
            )
        except Exception as e:
            log.warning("alert outlook failed: %s", e)
            view = AlertOutlook(
                pace_remaining_kwh=pace_remaining,
                pace_dt_s=pace_dt,
                soc_full_pct=float(s.soc_full_defense_threshold_pct),
            )
    else:
        view = view.model_copy(
            update={
                "pace_remaining_kwh": pace_remaining
                if view.pace_remaining_kwh is None
                else view.pace_remaining_kwh,
                "pace_dt_s": pace_dt if view.pace_dt_s is None else view.pace_dt_s,
            }
        )

    firings = evaluate(snap, view)
    to_push = apply_firings(
        state,
        firings,
        now=aware,
        debounce_min=int(s.anomaly_debounce_min),
        cooldown_min=int(s.anomaly_cooldown_min),
        clear_min=int(s.anomaly_clear_min),
    )
    _remember_pace(state, aware, remaining_kwh)
    state.prev_e_twc_kwh = E_twc_kwh
    state.prev_soc_pct = soc_pct
    state.prev_ts = _iso(aware)
    _write_state(state, p)

    if notify:
        for firing in to_push:
            send_ntfy(firing.title, firing.body)
    return to_push
