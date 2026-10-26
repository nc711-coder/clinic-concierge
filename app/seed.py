"""Create the booking tables and fill the calendar with bookable slots.

    python -m app.seed                  # next 21 days from today (keeps existing slots and bookings)
    python -m app.seed --days 42
    python -m app.seed --reset          # wipe all slots and appointments, then regenerate
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from ingest.env import load_dotenv


def main(argv=None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="python -m app.seed", description="Generate bookable appointment slots.")
    p.add_argument("--days", type=int, default=21)
    p.add_argument("--busy", type=int, default=35, help="percent of slots already taken by other patients")
    p.add_argument("--reset", action="store_true", help="delete all slots and appointments first")
    args = p.parse_args(argv)
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set (put it in .env).")
        return 2

    import psycopg
    from psycopg.rows import dict_row

    from .booking import load_schedule, seed_slots
    schedule = load_schedule()
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True, prepare_threshold=None)
    today = date.today()
    out = seed_slots(conn, schedule, today, days=args.days, busy_pct=args.busy, reset=args.reset)
    open_ = conn.execute("select count(*) n from slots where status = 'open' and starts_at > now()").fetchone()["n"]
    print(f"Slots created: {out['created']} ({out['already_taken']} marked as taken by other patients).")
    print(f"Open future slots: {open_}, {today:%b %d} to {args.days} days ahead, "
          f"{len(schedule.providers)} providers, timezone {schedule.timezone}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
