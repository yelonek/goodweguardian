"""Mapowanie HourPlan → exec_mode (względem wizji SOC) + zapis ``state/planner_output.json``."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from planner.battery import BatteryParams, max_power_for_hour
from planner.config import (
    PLANNER_OUTPUT_PATH,
    PLANNER_POLICY_VALID_MINUTES,
    PLANNER_SOC_MIN_PCT,
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

# Próg zmiany SOC [pp] — poniżej traktujemy godzinę jako „trzymaj stan”.
SOC_GAP_EPS_PCT = 1.0
# Minimalny spadek SOC [pp] dla ``export_profit`` (świadome rozładowanie zarobkowe).
# Mniejsze dipy (szum trackingu / wear) przy nadwyżce PV → soak (neutral), nie eksport.
SOC_GAP_DISCHARGE_PCT = 4.0
# Powyżej tego SOC uznajemy baterię za „pełną” — dopiero wtedy spill nadwyżki PV.
SOC_NEAR_FULL_PCT = 95.0
# Tolerancja „taniego importu” względem minimum horyzontu [PLN/kWh].
_CHEAP_IMPORT_TOL_PLN = 0.02
NET_NEUTRAL_EPS_KWH = 0.05
# Legacy alias — testy / importy zewnętrzne.
BATTERY_DELTA_EPS_KWH = 0.05

EXEC_MODE_LABELS_PL: dict[ExecMode, str] = {
    "export_profit": "eksport zarobkowy",
    "export_pv_surplus": "eksport PV",
    "neutral": "neutralny",
    "import_grid": "import z sieci",
    "charge_grid": "ładowanie z sieci",
}

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


def _pct_from_battery_delta(bd_kwh: float, hin: HourInputs | None = None) -> int:
    """Szacunek % mocy z planowanej Δ baterii (względem limitu slotu / h)."""
    if hin is not None and float(hin.hour_fraction) < 1.0 - 1e-9:
        cap = max_power_for_hour(hin, BatteryParams())
    else:
        cap = max_battery_kwh_per_hour()
    if cap <= 0:
        return 2
    pct = int(round(abs(bd_kwh) / cap * 100.0))
    return max(2, min(100, pct))


def _pct_from_soc_gap(gap_pct: float, hin: HourInputs | None = None) -> int:
    """Szacunek % mocy z |ΔSOC| → przybliżone kWh AC."""
    bp = BatteryParams()
    eta1 = bp.eta_one_way
    # |ΔSOC| → energia w magazynie → AC (ładowanie: E_ac = E_stored / η₁)
    stored_kwh = abs(gap_pct) / 100.0 * bp.capacity_kwh
    ac_kwh = stored_kwh / eta1 if eta1 > 0 else stored_kwh
    return _pct_from_battery_delta(ac_kwh, hin)


def _export_pv_surplus_viable(export_pln: float) -> bool:
    return export_pln > 0.0


def _pv_surplus_rem_kwh(hin: HourInputs | None) -> float:
    """Nadwyżka PV na resztę / pełną godzinę [kWh]."""
    if hin is None:
        return 0.0
    pv_so = float(hin.pv_so_far_kwh or 0.0)
    load_so = float(hin.load_so_far_kwh or 0.0)
    pv_rem = float(hin.pv_kwh) - pv_so
    load_rem = float(hin.load_kwh) - load_so
    return pv_rem - load_rem


def _soc_gap_ac_kwh(gap_pct: float) -> float:
    """Ile kWh AC potrzeba, by domknąć |gap| SOC (ładowanie)."""
    bp = BatteryParams()
    eta1 = bp.eta_one_way
    stored = abs(gap_pct) / 100.0 * bp.capacity_kwh
    return stored / eta1 if eta1 > 0 else stored


def _is_cheap_import(import_pln: float, cheap_import_threshold_pln: float | None) -> bool:
    if cheap_import_threshold_pln is None:
        return False
    return import_pln <= cheap_import_threshold_pln + 1e-9


def cheap_import_threshold_from_inputs(hour_inputs: list[HourInputs]) -> float | None:
    """Próg taniego importu horyzontu: min(import) + tolerancja."""
    if not hour_inputs:
        return None
    return min(float(h.import_pln_per_kwh) for h in hour_inputs) + _CHEAP_IMPORT_TOL_PLN


def map_hour_to_exec_mode(
    hp: HourPlan,
    hin: HourInputs | None = None,
    *,
    cheap_import_threshold_pln: float | None = None,
) -> HourPolicyRow:
    """Mapowanie wizji SOC (``soc_end_pct``) na jeden ``exec_mode`` + parametry.

    Intencja wynika z ``gap = soc* − soc0``, nie z jednoczesnych ``ch`` i ``exp``.
    """
    soc0 = float(hp.soc_start_pct)
    soc_star = float(hp.soc_end_pct)
    gap = soc_star - soc0
    bd = float(hp.battery_delta_kwh)
    net = float(hp.target_net_kwh)
    pv = float(hin.pv_kwh) if hin is not None else None
    load = float(hin.load_kwh) if hin is not None else None
    export_pln = float(hin.export_pln_per_kwh) if hin is not None else 0.0
    import_pln = float(hin.import_pln_per_kwh) if hin is not None else 1.11
    surplus = _pv_surplus_rem_kwh(hin)

    exec_mode: ExecMode
    discharge_pct: int | None = None
    charge_pct: int | None = None
    soc_floor_pct: float | None = None
    target_soc_pct: float | None = None
    allow_grid = False

    if gap > SOC_GAP_EPS_PCT:
        # Wizja: naładuj — soak PV; grid charge tylko w tanim oknie.
        need_ac = _soc_gap_ac_kwh(gap)
        shortfall = need_ac - max(0.0, surplus)
        if shortfall <= NET_NEUTRAL_EPS_KWH:
            exec_mode = "neutral"
        elif _is_cheap_import(import_pln, cheap_import_threshold_pln):
            exec_mode = "charge_grid"
            allow_grid = True
            charge_pct = (
                _pct_from_battery_delta(bd, hin)
                if abs(bd) > BATTERY_DELTA_EPS_KWH
                else _pct_from_soc_gap(gap, hin)
            )
            target_soc_pct = soc_star
        elif surplus > NET_NEUTRAL_EPS_KWH:
            # Częściowy soak z PV; bez eksportu nadwyżki i bez drogiego AC→baterii.
            exec_mode = "neutral"
        else:
            # Drogi import: bateria tylko z PV (DC), dom z sieci.
            exec_mode = "import_grid"
    elif gap < -SOC_GAP_DISCHARGE_PCT:
        # Świadome rozładowanie zarobkowe tylko gdy plan faktycznie sprzedaje (net > 0).
        # Sam spadek soc* przy net≈0 / nadwyżce PV to szum trackingu albo serve — soak/Flappy,
        # nie export_profit (który forsownie rozładowuje baterię do sieci).
        if net > NET_NEUTRAL_EPS_KWH:
            exec_mode = "export_profit"
            discharge_pct = (
                _pct_from_battery_delta(bd, hin)
                if abs(bd) > BATTERY_DELTA_EPS_KWH
                else _pct_from_soc_gap(gap, hin)
            )
            soc_floor_pct = max(float(PLANNER_SOC_MIN_PCT), soc_star)
        elif surplus > NET_NEUTRAL_EPS_KWH:
            exec_mode = "neutral"
        elif surplus < -NET_NEUTRAL_EPS_KWH and bd >= -BATTERY_DELTA_EPS_KWH:
            exec_mode = "import_grid"
        else:
            exec_mode = "neutral"
    else:
        # Płaski / drobny dip (|gap| < próg rozładowania) — nie mylić z export_profit.
        # Przy miejscu w baterii nadwyżka PV idzie w soak (neutral), nie w eksport.
        near_full = max(soc0, soc_star) >= SOC_NEAR_FULL_PCT - 1e-9
        if (
            surplus > NET_NEUTRAL_EPS_KWH
            and _export_pv_surplus_viable(export_pln)
            and near_full
        ):
            exec_mode = "export_pv_surplus"
        elif surplus < -NET_NEUTRAL_EPS_KWH and bd >= -BATTERY_DELTA_EPS_KWH:
            exec_mode = "import_grid"
        else:
            # Bilans / soak / drobny serve z baterii → Flappy.
            exec_mode = "neutral"

    return HourPolicyRow(
        date=hp.date,
        hour=hp.hour,
        exec_mode=exec_mode,
        policy=_EXEC_TO_LEGACY_POLICY.get(exec_mode),
        params=HourPolicyParams(
            target_net_kwh=net,
            battery_delta_kwh=bd,
            soc_end_pct=soc_star,
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
    *,
    cheap_import_threshold_pln: float | None = None,
) -> HourPolicyRow:
    return map_hour_to_exec_mode(
        hp, hin, cheap_import_threshold_pln=cheap_import_threshold_pln
    )


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
    cheap_thr = cheap_import_threshold_from_inputs(hour_inputs)
    rows = [
        map_hour_to_exec_mode(
            hp,
            by_slot.get((hp.date, hp.hour)),
            cheap_import_threshold_pln=cheap_thr,
        )
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
    cheap_thr = (
        cheap_import_threshold_from_inputs(hour_inputs) if hour_inputs else None
    )
    for hp in plan.hours:
        if hp.date == local_date and hp.hour == hour:
            hin = None
            if hour_inputs:
                for hi in hour_inputs:
                    if hi.date == local_date and hi.hour == hour:
                        hin = hi
                        break
            return map_hour_to_exec_mode(
                hp, hin, cheap_import_threshold_pln=cheap_thr
            )
    return None
