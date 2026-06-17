"""Modele audytowalne (Pydantic) — każda decyzja z wejściami i uzasadnieniem."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class HourInputs(BaseModel):
    """Prognozy i ceny użyte przy planowaniu jednej godziny."""

    date: str
    hour: int = Field(ge=0, le=23)
    load_kwh: float
    pv_kwh: float
    import_pln_per_kwh: float
    export_pln_per_kwh: float
    load_source: str = ""
    pv_source: str = ""
    # Pasma niepewności (optimizer CVaR); brak → używane pv_kwh / load_kwh.
    pv_kwh_p10: float | None = None
    pv_kwh_p90: float | None = None
    load_kwh_p75: float | None = None


class HourPlan(BaseModel):
    """Plan na godzinę: docelowy bilans netto na liczniku (eksport − import)."""

    date: str
    hour: int = Field(ge=0, le=23)
    target_net_kwh: float
    expected_cashflow_pln: float
    battery_wear_cost_pln: float = 0.0
    soc_start_pct: float
    soc_end_pct: float
    battery_delta_kwh: float


class DailyPlan(BaseModel):
    """Rolling plan — horyzont od bieżącej godziny do ostatniej z cenami."""

    schema_version: int = 2
    plan_id: str
    local_date: str
    generated_at: str
    timezone: str
    horizon_start: str = ""
    horizon_end: str = ""
    soc_start_pct: float
    expected_total_cashflow_pln: float
    optimizer: str
    inputs_snapshot: dict[str, Any]
    hours: list[HourPlan]


class AuditEvent(BaseModel):
    """Jeden wpis w łańcuchu audytu (append-only JSONL)."""

    schema_version: int = 1
    event_id: str
    ts_utc: str
    local_date: str
    kind: Literal[
        "plan_created",
        "day_audited",
        "hour_reconciled",
        "day_reviewed",
        "cvar_calibrated",
    ]
    plan_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class HourReconciliation(BaseModel):
    """Porównanie planu z rzeczywistością (telemetria)."""

    date: str
    hour: int
    plan_id_at_hour: str | None = None
    plan_generated_at: str | None = None
    planned_net_kwh: float | None
    actual_net_kwh: float | None
    planned_cashflow_pln: float | None
    actual_cashflow_pln: float | None
    forecast_load_kwh: float | None
    actual_load_kwh: float | None
    forecast_pv_kwh: float | None
    actual_pv_kwh: float | None
    counterfactual_optimal_pln: float | None
    gap_vs_optimal_pln: float | None
    notes: list[str] = Field(default_factory=list)


class HourAuditRow(BaseModel):
    """Jedna godzina w dziennym audycie."""

    hour: int = Field(ge=0, le=23)
    actual_net_kwh: float
    actual_cashflow_pln: float
    actual_load_kwh: float
    actual_pv_kwh: float
    optimal_net_kwh: float | None = None
    optimal_cashflow_pln: float | None = None
    gap_vs_optimal_pln: float | None = None


class DayAudit(BaseModel):
    """Dzienny audyt: fakty vs perfect foresight na telemetrii."""

    schema_version: int = 1
    local_date: str
    audited_at: str
    actual_total_cashflow_pln: float
    perfect_foresight_cashflow_pln: float | None
    uplift_vs_actual_pln: float | None
    hours: list[HourAuditRow]
    summary_pl: str


PlannerPolicyName = Literal[
    "hold_neutral",
    "hold_export",
    "hold_import",
    "charge",
    "discharge_export",
    "discharge_serve",
]

ExecMode = Literal[
    "export_profit",
    "export_pv_surplus",
    "neutral",
    "import_grid",
    "charge_grid",
]


class HourPolicyParams(BaseModel):
    """Parametry egzekucji dla jednej godziny (Guardian + dashboard)."""

    target_net_kwh: float
    battery_delta_kwh: float
    soc_end_pct: float
    pv_plan_kwh: float | None = None
    load_plan_kwh: float | None = None
    allow_grid_charge: bool = False
    discharge_pct: int | None = None
    charge_pct: int | None = None
    soc_floor_pct: float | None = None
    target_soc_pct: float | None = None


class HourPolicyRow(BaseModel):
    """Wiersz planu na godzinę — ``exec_mode`` + parametry dla Guardiana."""

    date: str
    hour: int = Field(ge=0, le=23)
    exec_mode: ExecMode
    params: HourPolicyParams
    policy: PlannerPolicyName | None = None


class PlannerPolicyArtifact(BaseModel):
    """Artefakt dla Guardiana / dashboardu — ``state/planner_output.json``."""

    schema_version: int = 2
    plan_id: str
    computed_at: str
    valid_until: str
    timezone: str
    degraded: bool = False
    hours: list[HourPolicyRow]


class DayReview(BaseModel):
    """@deprecated — użyj ``DayAudit``; zostawione dla starych plików review_*.json."""

    schema_version: int = 1
    local_date: str
    reviewed_at: str
    plan_id: str | None
    actual_total_cashflow_pln: float
    planned_total_cashflow_pln: float | None
    perfect_foresight_cashflow_pln: float | None
    uplift_vs_actual_pln: float | None
    uplift_vs_planned_pln: float | None
    hours: list[HourReconciliation]
    recommendations: list[str]
    summary_pl: str
