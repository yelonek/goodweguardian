"""Zapis i odczyt planów doby."""

from __future__ import annotations

import json
from pathlib import Path

from planner.config import PLANNER_PLANS_DIR, ensure_planner_dirs
from planner.models import DailyPlan


def plan_path(local_date: str) -> Path:
    ensure_planner_dirs()
    return PLANNER_PLANS_DIR / f"plan_{local_date}.json"


def save_plan(plan: DailyPlan) -> Path:
    path = plan_path(plan.local_date)
    path.write_text(
        json.dumps(plan.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_plan(local_date: str) -> DailyPlan | None:
    path = plan_path(local_date)
    if not path.exists():
        return None
    return DailyPlan.model_validate_json(path.read_text(encoding="utf-8"))
