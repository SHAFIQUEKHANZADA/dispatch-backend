"""Clean refresh of St. Charles repair orders: clear the current (stale-backlog)
ROs, then re-sync ONLY recent open ROs from myKaarma with the new mapping
(orderDate written-date, lifecycle statuses, recency filter).

    python refresh_stcharles_ros.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models import Dealer, RepairOrder
from app.mykaarma.connector import sync_repair_orders

STORE_KEY = "mcgrath_honda_stcharles"


async def main() -> None:
    async with SessionLocal() as s:
        d = (await s.execute(select(Dealer).where(Dealer.dealer_key == STORE_KEY))).scalar_one()
        before = (await s.execute(select(func.count()).where(RepairOrder.dealer_id == d.id))).scalar()
        await s.execute(delete(RepairOrder).where(RepairOrder.dealer_id == d.id))
        await s.commit()
        print(f"cleared {before} old ROs; re-syncing recent open ROs from myKaarma…")

        res = await sync_repair_orders(s, d.id)
        print("OK:", res.ok, "| MSG:", res.message)

        rows = (
            await s.execute(
                select(RepairOrder.status, func.count())
                .where(RepairOrder.dealer_id == d.id)
                .group_by(RepairOrder.status)
            )
        ).all()
        print("by status:", {st: c for st, c in rows})


if __name__ == "__main__":
    asyncio.run(main())
