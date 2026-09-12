"""Import a GHL sub-account billing export (Wallet & Transactions -> Export Center)
into esther_ai_spend, then re-roll the affected days so the dashboard's AI Spend
and Cost-per-Booking cards show the REAL billed figures.

The CSV is per sub-account (no store column), so you tell it which store:

    python import_ghl_billing.py --store mcgrath_honda_stcharles "2026-09-01_to_2026-09-11.csv"

`--store` is the esther_stores.key (or the exact store name). Nothing about a
store is hardcoded here — it is resolved from the DB.

Idempotent: it writes one row per (store, day, product) via upsert, so
re-importing the same or an overlapping export just refreshes the totals.
Credits (Recharge/Refund) are ignored — only spend is counted.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from app.db import engine
from esther_ingest import rollup

# GHL "Transaction Type" -> our esther_ai_spend.source slug.
# Which slugs count as "AI Spend" is decided in rollup() (esther_ingest.py).
TYPE_TO_SOURCE = {
    "Voice AI": "voice_ai",
    "Workflow - External AI Models": "workflow_ai",
    "Conversation AI": "conversation_ai",
    "Content AI": "content_ai",
    "Reviews AI": "reviews_ai",
    "Emails": "emails",
    "Email Notifications": "email_notifications",
    "SMS": "sms",
    "Messaging": "messaging",
    "Phone": "phone",
}
# Credits / top-ups are not spend — never count them.
CREDIT_TYPES = {"Recharge", "Refund", "Credit", "Wallet Recharge", "Complimentary Credit"}

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def parse_local_date(s: str) -> date:
    """'Sep 11th 2026, 11:32:49 PM' -> date(2026, 9, 11) (the day GHL bills it under)."""
    m = re.match(r"\s*([A-Za-z]{3})[a-z]*\s+(\d{1,2})\w*\s+(\d{4})", s)
    if not m:
        raise ValueError(f"unrecognised date: {s!r}")
    mo, da, yr = m.group(1), int(m.group(2)), int(m.group(3))
    return date(yr, _MONTHS[mo], da)


def slug_for(txn_type: str) -> str:
    if txn_type in TYPE_TO_SOURCE:
        return TYPE_TO_SOURCE[txn_type]
    return re.sub(r"[^a-z0-9]+", "_", txn_type.lower()).strip("_") or "other"


def read_csv(path: Path) -> dict[tuple[str, str], float]:
    """Sum spend per (local_date, source). Skips credits."""
    totals: dict[tuple[str, str], float] = defaultdict(float)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        need = {"Transaction Type", "Activity Date", "Amount"}
        missing = need - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV is missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            ttype = (row["Transaction Type"] or "").strip()
            if not ttype or ttype in CREDIT_TYPES:
                continue
            try:
                amt = float(row["Amount"])
            except (TypeError, ValueError):
                continue
            if amt <= 0:
                continue
            date = parse_local_date(row["Activity Date"])
            totals[(date, slug_for(ttype))] += amt
    return totals


async def run(store_arg: str, csv_path: Path) -> None:
    totals = read_csv(csv_path)
    if not totals:
        raise SystemExit("No spend rows found in the CSV.")

    async with engine.begin() as conn:
        pg = (await conn.get_raw_connection()).driver_connection

        store = await pg.fetchrow(
            "select id, name from esther_stores where key=$1 or name=$1", store_arg
        )
        if not store:
            rows = await pg.fetch("select key, name from esther_stores order by sort_order")
            opts = "\n".join(f"    {r['key']:<28} {r['name']}" for r in rows)
            raise SystemExit(f"No store matches {store_arg!r}. Available:\n{opts}")
        store_id, store_name = store["id"], store["name"]

        # upsert one row per (store, day, product): idempotent re-import
        await pg.executemany(
            """
            insert into esther_ai_spend (store_id, local_date, amount_usd, source)
            values ($1, $2::date, $3, $4)
            on conflict (store_id, local_date, source)
              do update set amount_usd = excluded.amount_usd
            """,
            [(store_id, date, round(amt, 2), source)
             for (date, source), amt in sorted(totals.items())],
        )

        days = sorted({date for (date, _src) in totals})
        for d in days:
            await rollup(pg, store_id, d)

    # report
    by_day_ai: dict[str, float] = defaultdict(float)
    AI = {"voice_ai", "workflow_ai", "reviews_ai", "conversation_ai", "content_ai"}
    for (date, source), amt in totals.items():
        if source in AI:
            by_day_ai[date] += amt
    print(f"Imported billing for: {store_name}")
    print(f"  {len(totals)} product-day rows across {len(days)} days "
          f"({days[0]} … {days[-1]})")
    print("  AI Spend now live per day:")
    for d in sorted(by_day_ai):
        print(f"    {d}   ${by_day_ai[d]:,.2f}")
    print(f"  Total AI spend in file: ${sum(by_day_ai.values()):,.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Import a GHL billing CSV into esther_ai_spend")
    ap.add_argument("--store", required=True, help="esther_stores.key or exact store name")
    ap.add_argument("csv", help="path to the exported billing CSV")
    args = ap.parse_args()
    path = Path(args.csv)
    if not path.exists():
        raise SystemExit(f"File not found: {path}")
    asyncio.run(_wrapped(args.store, path))


async def _wrapped(store_arg: str, path: Path) -> None:
    try:
        await run(store_arg, path)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    main()
