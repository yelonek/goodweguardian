"""CLI planera: ``uv run python -m planner plan|reconcile|review [--date YYYY-MM-DD]``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from planner.service import build_daily_plan, reconcile_day, review_day


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audytowalny planer energii (PLN)")
    parser.add_argument(
        "command",
        choices=["plan", "reconcile", "review"],
        help="plan=nowy plan doby; reconcile=godziny vs telemetria; review=co poprawić",
    )
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="YYYY-MM-DD (domyślnie dziś)",
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

    d = args.date
    if args.command == "plan":
        plan = build_daily_plan(local_date=d, soc_start_pct=args.soc)
        print(
            f"Plan {plan.plan_id[:8]}… {plan.local_date}: "
            f"oczekiwany cashflow {plan.expected_total_cashflow_pln:+.2f} PLN"
        )
    elif args.command == "reconcile":
        n = reconcile_day(local_date=d)
        print(f"Zrekonsyliowano {n} godzin dla {d or date.today()}.")
    else:
        text = review_day(local_date=d)
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
