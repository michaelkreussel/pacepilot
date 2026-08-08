"""Backfill Garmin health and recovery history for connected PacePilot accounts."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import GarminAccount  # noqa: E402
from app.services.garmin.client import connect_garmin_account  # noqa: E402
from app.services.garmin.health_backfill import (  # noqa: E402
    DEFAULT_OVERLAP_DAYS,
    MIN_HISTORY_DATE,
    sync_health_history,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, help="sync only this Garmin account")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--min-date", type=date.fromisoformat, default=MIN_HISTORY_DATE)
    parser.add_argument("--overlap-days", type=int, default=DEFAULT_OVERLAP_DAYS)
    parser.add_argument("--delay", type=float, default=0.75)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.account_id is not None and args.account_id < 1:
        raise SystemExit("--account-id must be positive")
    if args.min_date > args.date:
        raise SystemExit("--min-date must not be after --date")
    if args.overlap_days < 1 or args.delay < 0:
        raise SystemExit("--overlap-days must be positive and --delay non-negative")

    with SessionLocal() as session:
        query = select(GarminAccount.id).where(GarminAccount.connected_at.is_not(None))
        if args.account_id is not None:
            query = query.where(GarminAccount.id == args.account_id)
        account_ids = list(session.scalars(query.order_by(GarminAccount.id)))
    if not account_ids:
        print("No connected Garmin accounts found.")
        return 1

    for account_id in account_ids:
        with SessionLocal() as session:
            account = session.get(GarminAccount, account_id)
            if account is None:
                continue
            client = connect_garmin_account(session, account)
            result = sync_health_history(
                session,
                client,
                account.user_id,
                today=args.date,
                minimum=args.min_date,
                overlap_days=args.overlap_days,
                delay=args.delay,
            )
            print(
                f"Account {account.id}: {result.api_calls} API calls, "
                f"{result.unique_days_processed} calendar days updated"
            )
            for name, stats in result.resources.items():
                print(
                    f"  {name}: {stats.populated_days} populated, {stats.empty_days} empty, "
                    f"{stats.earliest or '-'} to {stats.latest or '-'}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
