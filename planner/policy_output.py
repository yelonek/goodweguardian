"""Mapowanie HourPlan → exec_mode + zapis ``state/planner_output.json``."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from planner.config import (
    PLANNER_EXPORT_PROFIT_MIN_PLN,
    PLANNER_OUTPUT_PATH,
    PLANNER_POLICY_VALID_MINUTES,
    ensure_planner_dirs,
    max_battery_kwh_per_hour,
)
from planner.models import (
    DailyPlan,
    ExecMode,
    HourInputs,
    HourPlan,
    HourPolicyParams,
    HourPolicyRow,
    PlannerPolicyArtifact,
    PlannerPolicyName,
)

log = logging.getLogger("planner")

BATTERY_DELTA_EPS_KWH = 0.05
NET_NEUTRAL_EPS_KWH = 0.05

EXEC_MODE_LABELS_PL: dict[ExecMode, str] = {
    "export_profit": "eksport zarobkowy",
    "export_pv_surplus": "eksport PV",
    "neutral": "neutralny",
    "import_grid": "import z sieci",
    "charge_grid": "ładowanie z sieci",
}

# Legacy dashboard / stare pliki JSON
POLICY_LABELS_PL: dict[PlannerPolicyName, str] = {
    "hold_neutral": "neutral",
    "hold_export": "eksport PV",
    "hold_import": "import",
    "charge": "ładuj",
    "discharge_export": "rozł.→sieć",
    "discharge_serve": "rozł.→dom",
}

_EXEC_TO_LEGACY_POLICY: dict[ExecMode, PlannerPolicyName] = {
    "export_profit": "discharge_export",
    "export_pv_surplus": "hold_export",
    "neutral": "hold_neutral",
    "import_grid": "hold_import",
    "charge_grid": "charge",
}


def _pct_from_battery_delta(bd_kwh: float) -> int:
    """Szacunek % mocy z planowanej zmiany baterii w godzinie."""
    cap = max_battery_kwh_per_hour()
    pct = int(round(abs(bd_kwh) / cap * 100.0))
    return max(2, min(100, pct))


def map_hour_to_exec_mode(
    hp: HourPlan,
    hin: HourInputs | None = None,
) -> HourPolicyRow:
    """Deterministyczne mapowanie wyniku optymalizatora na ``exec_mode`` + parametry."""
    bd = float(hp.battery_delta_kwh)
    net = float(hp.target_net_kwh)
    pv = float(hin.pv_kwh) if hin is not None else None
    load = float(hin.load_kwh) if hin is not None else None
    export_pln = float(hin.export_pln_per_kwh) if hin is not None else 0.0

    exec_mode: ExecMode
    discharge_pct: int | None = None
    charge_pct: int | None = None
    soc_floor_pct: float | None = None
    target_soc_pct: float | None = None
    allow_grid = False

    if abs(bd) <= BATTERY_DELTA_EPS_KWH:
        if net > NET_NEUTRAL_EPS_KWH:
            exec_mode = "export_pv_surplus"
        elif net < -NET_NEUTRAL_EPS_KWH:
            exec_mode = "import_grid"
        else:
            exec_mode = "neutral"
    elif bd > BATTERY_DELTA_EPS_KWH:
        exec_mode = "charge_grid"
        allow_grid = net < -NET_NEUTRAL_EPS_KWH
        charge_pct = _pct_from_battery_delta(bd)
        target_soc_pct = float(hp.soc_end_pct)
    elif net > NET_NEUTRAL_EPS_KWH and export_pln >= PLANNER_EXPORT_PROFIT_MIN_PLN:
        # Celowy eksport z baterii (dodatni net na liczniku).
        exec_mode = "export_profit"
        discharge_pct = _pct_from_battery_delta(bd)
        soc_floor_pct = float(hp.soc_start_pct)
    elif net > NET_NEUTRAL_EPS_KWH:
        exec_mode = "export_pv_surplus"
    elif net < -NET_NEUTRAL_EPS_KWH:
        # Import z sieci dominuje (net ujemny), nie eksport zarobkowy.
        exec_mode = "import_grid"
    else:
        # net ≈ 0, rozładowanie pokrywa load — bilans licznika neutralny (Flappy).
        exec_mode = "neutral"

    return HourPolicyRow(
        date=hp.date,
        hour=hp.hour,
        exec_mode=exec_mode,
        policy=_EXEC_TO_LEGACY_POLICY.get(exec_mode),
        params=HourPolicyParams(
            target_net_kwh=net,
            battery_delta_kwh=bd,
            soc_end_pct=float(hp.soc_end_pct),
            pv_plan_kwh=pv,
            load_plan_kwh=load,
            allow_grid_charge=allow_grid,
            discharge_pct=discharge_pct,
            charge_pct=charge_pct,
            soc_floor_pct=soc_floor_pct,
            target_soc_pct=target_soc_pct,
        ),
    )


def map_hour_to_policy(
    hp: HourPlan,
    hin: HourInputs | None = None,
) -> HourPolicyRow:
    """Alias zachowawczy — zwraca wiersz z ``exec_mode``."""
    return map_hour_to_exec_mode(hp, hin)


def exec_mode_label_pl(mode: ExecMode) -> str:
    return EXEC_MODE_LABELS_PL.get(mode, mode)


def policy_label_pl(policy: PlannerPolicyName) -> str:
    return POLICY_LABELS_PL.get(policy, policy)


def _inputs_by_slot(hour_inputs: list[HourInputs]) -> dict[tuple[str, int], HourInputs]:
    return {(h.date, h.hour): h for h in hour_inputs}


def build_policy_artifact(
    plan: DailyPlan,
    hour_inputs: list[HourInputs],
    *,
    degraded: bool = False,
    valid_minutes: int | None = None,
) -> PlannerPolicyArtifact:
    """Buduje artefakt policy dla całego horyzontu planu."""
    by_slot = _inputs_by_slot(hour_inputs)
    rows = [
        map_hour_to_exec_mode(hp, by_slot.get((hp.date, hp.hour)))
        for hp in plan.hours
    ]
    computed = datetime.fromisoformat(plan.generated_at.replace("Z", "+00:00"))
    if computed.tzinfo is None:
        computed = computed.replace(tzinfo=UTC)
    mins = valid_minutes if valid_minutes is not None else PLANNER_POLICY_VALID_MINUTES
    valid_until = computed + timedelta(minutes=mins)
    return PlannerPolicyArtifact(
        plan_id=plan.plan_id,
        computed_at=computed.isoformat(),
        valid_until=valid_until.isoformat(),
        timezone=plan.timezone,
        degraded=degraded,
        hours=rows,
    )


def save_policy_artifact(artifact: PlannerPolicyArtifact) -> None:
    """Atomowy zapis ``state/planner_output.json``."""
    ensure_planner_dirs()
    PLANNER_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(artifact.model_dump(), indent=2, ensure_ascii=False) + "\n"
    tmp = PLANNER_OUTPUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(PLANNER_OUTPUT_PATH)
    log.info(
        "policy artifact %s (%d h, degraded=%s) → %s",
        artifact.plan_id[:8],
        len(artifact.hours),
        artifact.degraded,
        PLANNER_OUTPUT_PATH,
    )


def load_policy_artifact() -> PlannerPolicyArtifact | None:
    if not PLANNER_OUTPUT_PATH.exists():
        return None
    try:
        raw = json.loads(PLANNER_OUTPUT_PATH.read_text(encoding="utf-8"))
        return _coerce_policy_artifact(raw)
    except Exception as e:
        log.warning("policy artifact read failed: %s", e)
        return None


def _legacy_policy_to_exec_mode(policy: str) -> ExecMode:
    mapping: dict[str, ExecMode] = {
        "hold_export": "export_pv_surplus",
        "hold_import": "import_grid",
        "hold_neutral": "neutral",
        "charge": "charge_grid",
        "discharge_export": "export_profit",
        "discharge_serve": "neutral",
    }
    return mapping.get(policy, "neutral")


def _coerce_policy_artifact(raw: dict) -> PlannerPolicyArtifact:
    """Migracja starych artefaktów (tylko ``policy``) → ``exec_mode``."""
    hours = raw.get("hours") or []
    for row in hours:
        if isinstance(row, dict) and "exec_mode" not in row and row.get("policy"):
            row["exec_mode"] = _legacy_policy_to_exec_mode(str(row["policy"]))
    return PlannerPolicyArtifact.model_validate(raw)


def policy_rows_by_slot(
    artifact: PlannerPolicyArtifact | None,
) -> dict[tuple[str, int], HourPolicyRow]:
    if artifact is None:
        return {}
    return {(r.date, r.hour): r for r in artifact.hours}


def policy_for_hour(
    plan: DailyPlan | None,
    local_date: str,
    hour: int,
    *,
    artifact: PlannerPolicyArtifact | None = None,
    hour_inputs: list[HourInputs] | None = None,
) -> HourPolicyRow | None:
    """Policy z artefaktu lub wyliczona z planu (gdy brak pliku policy)."""
    art = artifact if artifact is not None else load_policy_artifact()
    if art is not None and art.plan_id == (plan.plan_id if plan else ""):
        row = policy_rows_by_slot(art).get((local_date, hour))
        if row is not None:
            return row
    if plan is None:
        return None
    for hp in plan.hours:
        if hp.date == local_date and hp.hour == hour:
            hin = None
            if hour_inputs:
                for hi in hour_inputs:
                    if hi.date == local_date and hi.hour == hour:
                        hin = hi
                        break
            return map_hour_to_exec_mode(hp, hin)
    return None
