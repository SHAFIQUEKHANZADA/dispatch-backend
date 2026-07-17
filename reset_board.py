"""Reset the demo board so 'Make Smart Decision' has work to plan.

    python reset_board.py

Use this before a demo. It puts the shop back to the moment the seed created:
every demo RO returns to its original status and today's assignments are
cleared, so the board is full of Ready-to-Dispatch work again.

It does NOT touch:
  - the dealership, its settings, or the op-code map
  - technicians (including any you added by hand)
  - the 90-day RO history / familiarity map / comeback pairs
  - user_profiles  <-- your login keeps working

Safe to run as many times as you like.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Assignment, AuditLog, Dealer, RepairOrder

DEALER_NAME = "McGrath Honda of St. Charles"

# The statuses the seed originally gave each demo RO.
SEED_STATUS = {
    "4460": "READY_TO_DISPATCH",   # Odyssey A/C — waiting customer
    "4461": "READY_TO_DISPATCH",   # Prologue EV — HV/EV cert required
    "4462": "READY_TO_DISPATCH",   # Civic brakes — comeback
    "4463": "READY_TO_DISPATCH",   # Pilot lube — Lube team only
    "4464": "READY_TO_DISPATCH",   # Fit engine — heat case
    "4465": "READY_TO_DISPATCH",   # CR-V alignment
    "4466": "PENDING_AUTHORIZATION",
    "4467": "WAITING_ON_PARTS",
    "4468": "OPEN",
}


async def main() -> None:
    async with SessionLocal() as s:
        dealer = (
            await s.execute(select(Dealer).where(Dealer.name == DEALER_NAME))
        ).scalar_one_or_none()
        if dealer is None:
            print(f"Dealer '{DEALER_NAME}' not found — nothing to reset.")
            return

        # 1. clear every assignment (the frozen dispatch decisions)
        res = await s.execute(delete(Assignment).where(Assignment.dealer_id == dealer.id))
        cleared = res.rowcount or 0

        # 2. clear the dispatch audit entries so the log reads clean for the demo
        await s.execute(
            delete(AuditLog).where(
                AuditLog.dealer_id == dealer.id,
                AuditLog.action.in_(["DISPATCH", "DISPATCH_OVERRIDE", "SMART_DECISION_APPLIED"]),
            )
        )

        # 3. put every demo RO back to its seeded status
        ros = list(
            (
                await s.execute(select(RepairOrder).where(RepairOrder.dealer_id == dealer.id))
            ).scalars()
        )
        restored, left = 0, 0
        for ro in ros:
            want = SEED_STATUS.get(ro.ro_number)
            if want is None:
                # an RO you created yourself — park it as Ready to Dispatch so it
                # shows up on the board too, unless it's already been completed.
                if ro.status == "IN_PROGRESS":
                    ro.status = "READY_TO_DISPATCH"
                    left += 1
                continue
            if ro.status != want:
                ro.status = want
                restored += 1

        await s.commit()

        ready = sum(1 for ro in ros if ro.status == "READY_TO_DISPATCH")
        print()
        print("  Board reset for demo")
        print("  " + "-" * 44)
        print(f"  Assignments cleared     {cleared}")
        print(f"  Demo ROs restored       {restored}")
        if left:
            print(f"  Your own ROs re-opened  {left}")
        print(f"  Ready to Dispatch now   {ready}   <- Make Smart Decision has this many to plan")
        print()
        print("  Refresh the Dispatch Board and hit 'Make Smart Decision'.")
        print()


if __name__ == "__main__":
    asyncio.run(main())
