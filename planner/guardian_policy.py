"""Odczyt aktywnego wiersza policy dla Guardiana."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from planner.config import PLANNER_POLICY_VALID_MINUTES
from planner.models import HourPolicyRow, PlannerPolicyArtifact
from planner.policy_output import load_policy_artifact, policy_rows_by_slot


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def active_policy_row(
    local_date: date,
    hour: int,
    *,
    now: datetime | None = None,
    tz: str = "Europe/Warsaw",
) -> tuple[HourPolicyRow, PlannerPolicyArtifact] | None:
    """
    Wiersz policy dla (data, godzina), jeśli artefakt istnieje i jest ważny.

    ``valid_until`` porównywane z ``now`` w strefie artefaktu.
    """
    art = load_policy_artifact()
    if art is None:
        return None
    if art.degraded:
        return None

    ref = now if now is not None else datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
    try:
        valid_until = _parse_iso(art.valid_until)
        if valid_until.tzinfo is not None:
            valid_until = valid_until.astimezone(ZoneInfo(art.timezone or tz)).replace(
                tzinfo=None
            )
        computed = _parse_iso(art.computed_at)
        if computed.tzinfo is not None:
            computed = computed.astimezone(ZoneInfo(art.timezone or tz)).replace(
                tzinfo=None
            )
        # Artefakt bez ważnego valid_until — domyślnie PLANNER_POLICY_VALID_MINUTES od computed
        if art.valid_until and ref > valid_until:
            return None
    except (TypeError, ValueError):
        pass

    d_iso = local_date.isoformat()
    row = policy_rows_by_slot(art).get((d_iso, hour))
    if row is None:
        return None
    return row, art
