"""CLI planera: ``uv run python -m planner plan|audit [--date YYYY-MM-DD]``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from planner.service import audit_day, build_rolling_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audytowalny planer energii (PLN)")
    parser.add_argument(
        "command",
        choices=["plan", "audit"],
        help="plan=rolling plan (co ~10 min); audit=dzienny audyt fakty vs perfect foresight",
    )
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="YYYY-MM-DD dla audit (domyślnie dziś); plan ignoruje — zawsze od teraz",
    )
    parser.add_argument(
        "--soc",
        type=float,
        default=None,
        help="SOC startowy %% (domyślnie z telemetrii)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command == "plan":
        plan = build_rolling_plan(soc_start_pct=args.soc)
        if plan is None:
            print("Brak planu (brak slotów z cennikiem lub błąd wejść).")
            return 1
        print(
            f"Plan {plan.plan_id[:8]}… {plan.horizon_start} → {plan.horizon_end}: "
            f"oczekiwany cashflow {plan.expected_total_cashflow_pln:+.2f} PLN "
            f"({len(plan.hours)} h)"
        )
    else:
        text = audit_day(local_date=args.date)
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
