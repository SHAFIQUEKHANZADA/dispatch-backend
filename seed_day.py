"""Seed a realistic dispatched DAY + a full Ready-to-Dispatch board.

    python seed_day.py

Creates REAL rows so the whole app looks like a live shop:
  - completed morning work + in-progress + queued (the Dashboard timeline)
  - a set of Ready-to-Dispatch ROs matching the dispatch board (heat case,
    comeback, A/C, lube, etc.) so Available ROs is populated and rankable
  - refreshes the DMS import timestamp so the Scoreboard's staleness gate passes

Pins the demo clock to ~1:30 PM local. Safe to re-run.
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import delete, select, update
from zoneinfo import ZoneInfo

from app.db import SessionLocal
from app.models import (
    Assignment,
    Dealer,
    ImportRun,
    RepairOrder,
    ROHistory,
    ROLine,
    Technician,
)

DEALER_NAME = "McGrath Honda of St. Charles"
TZ = ZoneInfo("America/Chicago")
RNG = random.Random(4471)

CATS = [
    ("Maintenance", "MAINTENANCE", "C", "LOF", "Multi-point inspection"),
    ("Brakes", "BRAKES", "B", "BRKFRT", "Front brake service"),
    ("Electrical/AC", "HVAC", "B", "ACDIAG", "A/C performance check"),
    ("Drivability/Diag", "DIAGNOSTIC", "A", "DRDIAG", "Drivability diagnosis"),
    ("Suspension", "ALIGNMENT", "B", "SUSALN", "Alignment"),
]
VEHICLES = [(2019, "Honda", "Odyssey"), (2021, "Honda", "CR-V"), (2022, "Honda", "Accord"),
            (2020, "Honda", "Pilot"), (2018, "Honda", "Civic")]

# The Ready-to-Dispatch board — modelled on the dispatcher mockup.
# (number, year, make, model, mileage, flags, priority, category, work_type,
#  tier, est_hours, [concern lines], promise_hour)
READY = [
    ("4432", 2016, "Honda", "Civic", 155300, ["HEAT_CASE", "COMEBACK"], "HIGH",
     "Drivability/Diag", "DIAGNOSTIC", "A", 2.0,
     ["Customer states: intermittent stall", "Restarts after sitting", "Possible fuel/ignition"], 17),
    ("4436", 2019, "Honda", "Odyssey", 88700, ["MGR_FLAG"], "MEDIUM",
     "Electrical/AC", "HVAC", "B", 2.0,
     ["A/C blowing warm", "Evac & recharge", "Check for leaks"], 16),
    ("4439", 2021, "Honda", "Civic", 22050, ["WAITING"], "MEDIUM",
     "Maintenance", "LUBE", "C", 0.6,
     ["LOF + tire rotation", "Multi-point inspection", "Customer waiting"], 16),
    ("4441", 2020, "Honda", "Pilot", 64100, [], "MEDIUM",
     "Brakes", "BRAKES", "B", 1.8,
     ["Brake noise, front", "Inspect pads & rotors"], 17),
    ("4444", 2018, "Honda", "Accord", 121300, ["COMEBACK"], "MEDIUM",
     "Suspension", "ALIGNMENT", "B", 1.5,
     ["Pulls right under braking", "Four-wheel alignment"], 17),
    ("4447", 2022, "Honda", "CR-V", 18900, [], "LOW",
     "Maintenance", "MAINTENANCE", "C", 0.5,
     ["Oil & filter change", "Tire rotation"], 18),
]

LUBE_TEAM_RO = {"4439"}  # requires the Lube team


def anchor_now() -> datetime:
    d = datetime.now(timezone.utc).astimezone(TZ).date()
    while d.weekday() > 4:
        d -= timedelta(days=1)
    return datetime.combine(d, time(13, 30), tzinfo=TZ).astimezone(timezone.utc)


def write_demo_now(dt: datetime) -> None:
    path = os.path.join(os.path.dirname(__file__), ".env")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = [l.rstrip("\n") for l in fh if not l.startswith("DEMO_NOW=")]
    lines.append(f"DEMO_NOW={dt.isoformat()}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


async def main() -> None:
    now = anchor_now()
    write_demo_now(now)
    local_now = now.astimezone(TZ)

    async with SessionLocal() as s:
        dealer = (await s.execute(select(Dealer).where(Dealer.name == DEALER_NAME))).scalar_one_or_none()
        if dealer is None:
            print("Run seed.py first."); return
        did = dealer.id

        # seed_day OWNS the current-day board: wipe every open/current RO for the
        # dealer and rebuild the whole day, so the dashboard, Available ROs, techs
        # and route sheet all describe one coherent day (no stragglers from an
        # earlier seed). The 90-day ROHistory (used by the scoreboard + engine) is
        # a separate table and is left untouched.
        old = list((await s.execute(select(RepairOrder.id).where(
            RepairOrder.dealer_id == did))).scalars())
        await s.execute(delete(Assignment).where(Assignment.dealer_id == did))
        if old:
            await s.execute(delete(ROLine).where(ROLine.ro_id.in_(old)))
            await s.execute(delete(RepairOrder).where(RepairOrder.id.in_(old)))

        # keep the 90-day import current so the scoreboard passes the staleness gate
        await s.execute(update(ImportRun).where(ImportRun.dealer_id == did).values(completed_at=now))
        await s.execute(update(ROHistory).where(ROHistory.dealer_id == did).values(imported_at=now))
        await s.commit()

        # Build the day's board on a clean ~10-tech crew (8 Main + 2 Lube) so the
        # dashboard + route sheet read like the mockup — not all 47 real techs at
        # once. The rest stay active for the scoreboard + dispatch ranking.
        mains = list((await s.execute(select(Technician).where(
            Technician.dealer_id == did, Technician.active.is_(True),
            Technician.team == "Main").order_by(Technician.name).limit(8))).scalars())
        lubes = list((await s.execute(select(Technician).where(
            Technician.dealer_id == did, Technician.active.is_(True),
            Technician.team == "Lube").order_by(Technician.name).limit(2))).scalars())
        techs = mains + lubes

        seq = 6000
        made_ro = made_asn = 0

        def new_ro(prefix, cat, wt, tier, hrs, status, lines, when):
            nonlocal seq, made_ro
            seq += 1
            yr, mk, md = RNG.choice(VEHICLES)
            ro = RepairOrder(
                dealer_id=did, ro_number=f"{seq}", vehicle_year=yr, vehicle_make=mk,
                vehicle_model=md, mileage=RNG.randint(20000, 120000), concern_category=cat,
                work_type=wt, tier=tier, est_hours=hrs, status=status,
                written_at=when, promise_at=when + timedelta(hours=4))
            ro.lines = [ROLine(dealer_id=did, op_code=lines[0], description=lines[1], flagged_hours=hrs)]
            s.add(ro)
            made_ro += 1
            return ro

        # per-tech plan: (fill morning?, in_progress?, queued, idle?)
        plans = [
            (True, True, 0, False), (True, True, 1, False), (True, False, 0, False),
            (True, True, 0, False), (True, False, 1, False), (True, True, 0, False),
            (False, False, 0, True), (True, True, 1, False), (True, False, 0, False),
            (True, True, 0, False),
        ]

        for idx, t in enumerate(techs):
            fill, in_prog, n_queued, idle = plans[idx % len(plans)]
            if not (t.shift_start and t.work_days):
                continue
            shift_start = datetime.combine(local_now.date(), t.shift_start, tzinfo=TZ)
            avail_min = (local_now - shift_start).total_seconds() / 60.0
            if t.lunch_start and t.lunch_end and local_now.time() > t.lunch_end:
                avail_min -= (datetime.combine(local_now.date(), t.lunch_end) -
                              datetime.combine(local_now.date(), t.lunch_start)).total_seconds() / 60.0
            if in_prog:
                avail_min -= 60
            if idle:
                avail_min = min(avail_min, 90)

            cursor = shift_start
            filled = 0.0
            if fill:
                while filled < max(0, avail_min) - 20:
                    cat, wt, tier, op, desc = RNG.choice(CATS)
                    hrs = RNG.choice([0.5, 0.8, 1.0, 1.0, 1.3, 1.5, 2.0])
                    if filled + hrs * 60 > avail_min:
                        hrs = max(0.5, round((avail_min - filled) / 60.0, 1))
                    ro = new_ro("H", cat, wt, tier, hrs, "COMPLETED", (op, desc), cursor)
                    await s.flush()
                    actual = hrs * RNG.choice([0.7, 0.75, 0.8, 0.85, 0.9, 1.0, 1.0])
                    s.add(Assignment(dealer_id=did, ro_id=ro.id, technician_id=t.id, match_score=90,
                        score_reasons=[], score_confident=True, recommended_rank=1, was_ai_recommendation=True,
                        assigned_at=cursor, started_at=cursor, completed_at=cursor + timedelta(hours=actual)))
                    cursor += timedelta(hours=actual + 0.05)
                    filled += hrs * 60
                    made_asn += 1

            if in_prog:
                cat, wt, tier, op, desc = RNG.choice(CATS)
                ro = new_ro("I", cat, wt, tier, RNG.choice([1.0, 1.5, 2.0]), "IN_PROGRESS", (op, desc), now)
                await s.flush()
                # vary how long they've been on it so the route-sheet green bars
                # fill to different widths (matches the mockup)
                started_ago = RNG.choice([15, 25, 40, 55, 70, 85])
                s.add(Assignment(dealer_id=did, ro_id=ro.id, technician_id=t.id, match_score=92,
                    score_reasons=[], score_confident=True, recommended_rank=1, was_ai_recommendation=True,
                    assigned_at=now - timedelta(minutes=started_ago), started_at=now - timedelta(minutes=started_ago), completed_at=None))
                made_asn += 1

            for _ in range(n_queued):
                cat, wt, tier, op, desc = RNG.choice(CATS)
                ro = new_ro("Q", cat, wt, tier, RNG.choice([0.8, 1.0, 1.3]), "IN_PROGRESS", (op, desc), now)
                await s.flush()
                s.add(Assignment(dealer_id=did, ro_id=ro.id, technician_id=t.id, match_score=88,
                    score_reasons=[], score_confident=True, recommended_rank=1, was_ai_recommendation=True,
                    assigned_at=now, started_at=None, completed_at=None))
                made_asn += 1

        # ---- carry-overs from yesterday (finished first thing this morning) ----
        # Written before today's day-start, closed out in the first hour of the
        # shift. The route sheet groups these under "CARRIED OVER FROM YESTERDAY".
        yday = now - timedelta(days=1)
        day_start = datetime.combine(local_now.date(), time(0, 0), tzinfo=TZ)
        carry = [
            ("Transmission", "WARRANTY", "A", 2.5, "TRANS", "Trans shudder — parts held overnight (carry)"),
            ("EV/Hybrid", "WARRANTY", "A", 3.0, "HVBATT", "HV battery cooling recall 24V-123 (carry)"),
            ("Drivability/Diag", "CUSTOMER", "A", 1.5, "DIAG", "No-start diagnosis, continued from yesterday"),
        ]
        for i, (cat, wt, tier, hrs, op, desc) in enumerate(carry):
            if i >= len(techs):
                break
            t = techs[i]
            ro = new_ro("CO", cat, wt, tier, hrs, "COMPLETED", (op, desc),
                        yday - timedelta(hours=RNG.uniform(2, 6)))
            await s.flush()
            done_at = day_start + timedelta(hours=1, minutes=15 + i * 20)
            s.add(Assignment(dealer_id=did, ro_id=ro.id, technician_id=t.id, match_score=91,
                score_reasons=[], score_confident=True, recommended_rank=1, was_ai_recommendation=True,
                assigned_at=yday, started_at=day_start.astimezone(timezone.utc),
                completed_at=done_at.astimezone(timezone.utc)))
            made_asn += 1

        # ---- the Ready-to-Dispatch board (his mockup) ----
        for (num, yr, mk, md, mi, flags, prio, cat, wt, tier, hrs, lines, promise_h) in READY:
            ro = RepairOrder(
                dealer_id=did, ro_number=num, vehicle_year=yr, vehicle_make=mk, vehicle_model=md,
                mileage=mi, concern_category=cat, work_type=wt, tier=tier, est_hours=hrs,
                required_team=("Lube" if num in LUBE_TEAM_RO else None),
                status="READY_TO_DISPATCH", flags=flags, priority=prio,
                written_at=now - timedelta(hours=RNG.uniform(1.5, 3.5)),
                promise_at=datetime.combine(local_now.date(), time(promise_h, 0), tzinfo=TZ).astimezone(timezone.utc))
            ro.lines = [ROLine(dealer_id=did, op_code=cat[:6].upper(), description=d, flagged_hours=round(hrs / len(lines), 1), sort_order=i)
                        for i, d in enumerate(lines)]
            s.add(ro)
            made_ro += 1

        await s.commit()
        ready = (await s.execute(select(RepairOrder).where(
            RepairOrder.dealer_id == did, RepairOrder.status == "READY_TO_DISPATCH"))).scalars().all()
        print(f"\n  Seeded full board.")
        print(f"  ROs created            {made_ro}")
        print(f"  Assignments            {made_asn}")
        print(f"  Ready to Dispatch NOW  {len(ready)}   <- Available ROs board")
        print(f"  Demo clock             {now.isoformat()}  ({local_now.strftime('%I:%M %p').lstrip('0')} local)")
        print("\n  Refresh Available ROs + Dashboard.\n")


if __name__ == "__main__":
    asyncio.run(main())
