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


class HourPlan(BaseModel):
    """Plan na godzinę: docelowy bilans netto na liczniku (eksport − import)."""

    date: str
    hour: int = Field(ge=0, le=23)
    target_net_kwh: float
    expected_cashflow_pln: float
    soc_start_pct: float
    soc_end_pct: float
    battery_delta_kwh: float


class DailyPlan(BaseModel):
    """Pełny plan doby — zapisany atomowo do audytu."""

    schema_version: int = 1
    plan_id: str
    local_date: str
    generated_at: str
    timezone: str
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
        "hour_reconciled",
        "day_reviewed",
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


class DayReview(BaseModel):
    """Codzienna retrospektywa — odpowiedź na „co możemy poprawić?”."""

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
