"""Remove the seeded DEMO repair orders for St. Charles, leaving ONLY the real
myKaarma-synced ones. The demo ROs are exactly those in IN_PROGRESS / COMPLETED
/ READY_TO_DISPATCH (the live sync only produces OPEN / WAITING_ON_PARTS).
Child rows (lines, assignments) cascade automatically.

    python clear_demo_ros.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models import Dealer, RepairOrder

DEMO_STATUSES = ("IN_PROGRESS", "COMPLETED", "READY_TO_DISPATCH")
STORE_KEY = "mcgrath_honda_stcharles"


async def main() -> None:
    async with SessionLocal() as s:
        d = (await s.execute(select(Dealer).where(Dealer.dealer_key == STORE_KEY))).scalar_one()
        before = (await s.execute(select(func.count()).where(RepairOrder.dealer_id == d.id))).scalar()
        await s.execute(
            delete(RepairOrder).where(
                RepairOrder.dealer_id == d.id,
                RepairOrder.status.in_(DEMO_STATUSES),
            )
        )
        await s.commit()
        after = (await s.execute(select(func.count()).where(RepairOrder.dealer_id == d.id))).scalar()
        rows = (
            await s.execute(
                select(RepairOrder.status, func.count())
                .where(RepairOrder.dealer_id == d.id)
                .group_by(RepairOrder.status)
            )
        ).all()
        print(f"removed {before - after} demo ROs  |  {after} real ROs remain")
        print("by status:", {st: c for st, c in rows})


if __name__ == "__main__":
    asyncio.run(main())
