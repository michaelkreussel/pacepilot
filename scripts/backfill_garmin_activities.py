"""Backfill complete Garmin activity history for connected PacePilot accounts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import GarminAccount  # noqa: E402
from app.services.garmin.activity_backfill import sync_activity_history  # noqa: E402
from app.services.garmin.client import connect_garmin_account  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, help="sync only this Garmin account")
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--page-size", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.account_id is not None and args.account_id < 1:
        raise SystemExit("--account-id must be positive")
    if args.delay < 0 or not 1 <= args.page_size <= 1000:
        raise SystemExit("--delay must be non-negative and --page-size between 1 and 1000")

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
            result = sync_activity_history(
                session,
                client,
                account.user_id,
                delay=args.delay,
                page_size=args.page_size,
            )
            print(
                f"Account {account.id}: Garmin has {result.remote_count} activities; "
                f"{result.inserted} inserted, {result.updated} updated, "
                f"{result.skipped} unchanged"
            )
            print(
                f"  Range: {result.oldest or '-'} to {result.newest or '-'}; "
                f"{result.details_stored} detail files; {result.api_calls} API calls"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
