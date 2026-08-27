"""Sync recent open ROs from myKaarma for ALL stores, one after another.
Resilient (retries + batched commits) so a flaky network won't lose progress;
safe to re-run — it upserts by RO number and resumes where it left off.

    python sync_all_stores.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Dealer, RepairOrder
from app.mykaarma.connector import sync_repair_orders

KEYS = [
    "mcgrath_honda_stcharles",
    "mcgrath_kia_stcharles",
    "mcgrath_honda_elgin",
    "mcgrath_acura_mortongrove",
    "mcgrath_acura_libertyville",
    "mcgrath_volvo_barrington",
    "audi_mortongrove",
]


async def main() -> None:
    async with SessionLocal() as s:
        for key in KEYS:
            d = (await s.execute(select(Dealer).where(Dealer.dealer_key == key))).scalar_one_or_none()
            if d is None:
                print(f"{key}: no dealer row — skipped")
                continue
            try:
                res = await sync_repair_orders(s, d.id)
                rows = (
                    await s.execute(
                        select(RepairOrder.status, func.count())
                        .where(RepairOrder.dealer_id == d.id)
                        .group_by(RepairOrder.status)
                    )
                ).all()
                print(f"{key}: {res.message}  ->  {dict((st, c) for st, c in rows)}")
            except Exception as e:  # noqa: BLE001 — keep going to the next store
                print(f"{key}: FAILED ({type(e).__name__}) — re-run to resume")
    print("\nAll stores attempted.")


if __name__ == "__main__":
    asyncio.run(main())
