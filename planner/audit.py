"""Append-only audyt JSONL — każde zdarzenie z UUID i timestampem."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from planner.config import PLANNER_AUDIT_DIR, ensure_planner_dirs
from planner.models import AuditEvent


def _audit_path(local_date: str) -> str:
    ensure_planner_dirs()
    return str(PLANNER_AUDIT_DIR / f"audit_{local_date}.jsonl")


def append_audit(event: AuditEvent) -> None:
    path = _audit_path(event.local_date)
    line = event.model_dump_json() + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


def new_event(
    *,
    local_date: str,
    kind: str,
    plan_id: str | None = None,
    payload: dict | None = None,
) -> AuditEvent:
    now = datetime.now(UTC)
    return AuditEvent(
        event_id=str(uuid.uuid4()),
        ts_utc=now.isoformat(),
        local_date=local_date,
        kind=kind,  # type: ignore[arg-type]
        plan_id=plan_id,
        payload=payload or {},
    )


def read_audit_events(local_date: str) -> list[AuditEvent]:
    path = PLANNER_AUDIT_DIR / f"audit_{local_date}.jsonl"
    if not path.exists():
        return []
    out: list[AuditEvent] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(AuditEvent.model_validate_json(line))
            except Exception:
                continue
    return out
