"""Mapowanie HourPlan → policy + zapis ``state/planner_output.json``."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from planner.config import (
    PLANNER_OUTPUT_PATH,
    PLANNER_POLICY_VALID_MINUTES,
    ensure_planner_dirs,
)
from planner.models import (
    DailyPlan,
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

POLICY_LABELS_PL: dict[PlannerPolicyName, str] = {
    "hold_neutral": "neutral",
    "hold_export": "eksport PV",
    "hold_import": "import",
    "charge": "ładuj",
    "discharge_export": "rozł.→sieć",
    "discharge_serve": "rozł.→dom",
}


def map_hour_to_policy(
    hp: HourPlan,
    hin: HourInputs | None = None,
) -> HourPolicyRow:
    """
    Deterministyczne mapowanie wyniku optymalizatora na jedną z 6 policy.

    Używa ``battery_delta_kwh`` (kierunek baterii) i ``target_net_kwh`` (cel licznika).
    """
    bd = float(hp.battery_delta_kwh)
    net = float(hp.target_net_kwh)
    pv = float(hin.pv_kwh) if hin is not None else None
    load = float(hin.load_kwh) if hin is not None else None

    if abs(bd) <= BATTERY_DELTA_EPS_KWH:
        if net > NET_NEUTRAL_EPS_KWH:
            policy: PlannerPolicyName = "hold_export"
        elif net < -NET_NEUTRAL_EPS_KWH:
            policy = "hold_import"
        else:
            policy = "hold_neutral"
        allow_grid = False
    elif bd > BATTERY_DELTA_EPS_KWH:
        policy = "charge"
        allow_grid = net < -NET_NEUTRAL_EPS_KWH
    elif net > NET_NEUTRAL_EPS_KWH:
        policy = "discharge_export"
        allow_grid = False
    else:
        policy = "discharge_serve"
        allow_grid = False

    return HourPolicyRow(
        date=hp.date,
        hour=hp.hour,
        policy=policy,
        params=HourPolicyParams(
            target_net_kwh=net,
            battery_delta_kwh=bd,
            soc_end_pct=float(hp.soc_end_pct),
            pv_plan_kwh=pv,
            load_plan_kwh=load,
            allow_grid_charge=allow_grid,
        ),
    )


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
        map_hour_to_policy(hp, by_slot.get((hp.date, hp.hour)))
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
        return PlannerPolicyArtifact.model_validate_json(
            PLANNER_OUTPUT_PATH.read_text(encoding="utf-8")
        )
    except Exception as e:
        log.warning("policy artifact read failed: %s", e)
        return None


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
            return map_hour_to_policy(hp, hin)
    return None
