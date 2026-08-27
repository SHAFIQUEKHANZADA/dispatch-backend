"""Sync a store's recent open ROs from myKaarma into the dispatch board.
Run when the network is stable. Usage:

    python sync_store_ros.py mcgrath_honda_stcharles
    python sync_store_ros.py mcgrath_kia_stcharles
    python sync_store_ros.py mcgrath_honda_elgin
    python sync_store_ros.py mcgrath_acura_mortongrove
    python sync_store_ros.py mcgrath_acura_libertyville

No arg defaults to St. Charles. Safe to re-run (upserts by RO number).
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Dealer, RepairOrder
from app.mykaarma.connector import sync_repair_orders


async def main(key: str) -> None:
    async with SessionLocal() as s:
        d = (await s.execute(select(Dealer).where(Dealer.dealer_key == key))).scalar_one_or_none()
        if d is None:
            print(f"No store with key '{key}'.")
            return
        res = await sync_repair_orders(s, d.id)
        print(f"{key}: {res.message}")
        rows = (
            await s.execute(
                select(RepairOrder.status, func.count())
                .where(RepairOrder.dealer_id == d.id)
                .group_by(RepairOrder.status)
            )
        ).all()
        print("  by status:", {st: c for st, c in rows})


if __name__ == "__main__":
    store = sys.argv[1] if len(sys.argv) > 1 else "mcgrath_honda_stcharles"
    asyncio.run(main(store))
