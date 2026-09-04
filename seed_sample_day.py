"""Additive, reversible SAMPLE-DAY seed for the demo store (St. Charles).

Why: the owner reviews an empty-looking board and can't see the color system.
This drops a realistic mix of activity onto the demo day so every color shows —
completed (blue), in-progress (green), queued (light-green), and naturally some
idle (red) and no-plan techs — using REAL Ready-to-Dispatch ROs, not fabricated
ones. The board's status colors are driven by assignment STATE, not timestamps,
so this just sets started_at/completed_at appropriately.

SAFE: only touches techs that have NO assignment on the demo day, never deletes
existing data. Every row it creates is tagged with SENTINEL in `assigned_by`, so
`python seed_sample_day.py --clear` removes exactly what this script added.

Run:  python seed_sample_day.py          # seed
      python seed_sample_day.py --clear   # undo
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import delete, select
from zoneinfo import ZoneInfo

from app.clock import default_now
from app.db import SessionLocal, engine
from app.models import Assignment, ComebackPairRow, Dealer, RepairOrder, Technician

ST_CHARLES = uuid.UUID("d8af9a1a-f953-4784-8f0f-594e4bd8faae")
# Fixed marker so seeded rows are identifiable and cleanly removable.
SENTINEL = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
# Seeded comebacks carry this prefix on repeat_ro_number so --clear can remove them.
CB_PREFIX = "SEED-CB-"

# Per-tech "profile" giving each seeded tech a realistic efficiency + a quality
# signal, so the Reports (Mentor Board, Speed vs Quality, Match Payoff) show a
# real spread instead of everyone looking elite. (skill = efficiency as a ratio;
# comeback = whether one of their completed jobs comes back.)
#   solid    -> strong + clean          rushing -> fast BUT comeback-prone
#   coaching -> below 100%, room to grow
# returns (skill = efficiency ratio, wants_comeback, match_base = 3D fit score).
# match tracks FIT/QUALITY, not raw speed: a rushing tech is fast but a poor fit
# for that job (and it comes back), so the system scored them LOW — which is why
# high-match jobs end up more efficient AND cleaner (the Match Payoff story).
def _profile(idx: int) -> tuple[float, bool, int]:
    m = idx % 7
    if m == 0:      # coaching: slow, room to grow, low fit
        return 0.82 + (idx % 3) * 0.03, (idx % 2 == 0), 63
    if m == 1:      # rushing: quick but comeback-prone -> the system rated them low
        return 1.18 + (idx % 3) * 0.05, True, 71
    return 1.14 + (idx % 5) * 0.08, False, 89   # solid: fastest, clean, high fit

# Per-tech day recipes. Each letter is one job:
#   C = completed (blue, with an efficiency gain), P = in-progress (green),
#   Q = queued (light-green). Every recipe has a P or Q so the tech reads as
#   working (not idle). We seed MOST free techs and deliberately leave a HANDFUL
#   unseeded so a few rows show the red IDLE "needs work" money-signal — a
#   balanced, colorful board rather than a sea of one color.
RECIPES = [
    "CCPQ", "CPQ", "CCP", "PQ", "CPQ", "CCPQ", "CP", "CCQ", "PQQ", "CCP", "CPQ", "CCPP",
]
# Leave this many on-shift techs unseeded -> a couple of red IDLE rows (the money
# signal) while the rest of the board reads as a busy, working shop.
LEAVE_IDLE = 2


async def clear(db) -> int:
    await db.execute(
        delete(ComebackPairRow).where(
            ComebackPairRow.dealer_id == ST_CHARLES,
            ComebackPairRow.repeat_ro_number.like(f"{CB_PREFIX}%"),
        )
    )
    res = await db.execute(
        delete(Assignment).where(
            Assignment.dealer_id == ST_CHARLES, Assignment.assigned_by == SENTINEL
        )
    )
    await db.commit()
    return res.rowcount or 0


async def main(do_clear: bool) -> None:
    async with SessionLocal() as db:
        dealer = await db.get(Dealer, ST_CHARLES)
        tz = ZoneInfo(dealer.timezone or "America/Chicago")
        now = default_now()
        day = now.astimezone(tz).date()
        day_name = now.astimezone(tz).strftime("%a")

        if do_clear:
            n = await clear(db)
            print(f"Removed {n} seeded sample-day assignments.")
            await engine.dispose()
            return

        removed = await clear(db)  # idempotent: clear our own prior seed first
        if removed:
            print(f"(re-run) cleared {removed} previously-seeded rows")

        day_start = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
        day_end = day_start + timedelta(days=1)

        techs = list(
            (
                await db.execute(
                    select(Technician).where(
                        Technician.dealer_id == ST_CHARLES, Technician.active.is_(True)
                    ).order_by(Technician.name)
                )
            ).scalars()
        )
        on_shift = [
            t for t in techs
            if {d.strip()[:3].title() for d in (t.work_days or [])} >= {day_name}
            and t.shift_start and t.shift_end
        ]

        # Clean slate for the demo day: remove ANY existing assignment on this day
        # for on-shift techs (stale/idle leftovers otherwise float to the top of
        # the "sorted by attention" board and make a busy shop look empty). We then
        # give every on-shift tech a fresh full day below.
        onshift_ids = [t.id for t in on_shift]
        if onshift_ids:
            wiped = await db.execute(
                delete(Assignment).where(
                    Assignment.dealer_id == ST_CHARLES,
                    Assignment.assigned_at >= day_start,
                    Assignment.assigned_at < day_end,
                    Assignment.technician_id.in_(onshift_ids),
                )
            )
            if wiped.rowcount:
                print(f"(cleared {wiped.rowcount} existing demo-day rows for a clean board)")
        free_techs = on_shift

        ros = list(
            (
                await db.execute(
                    select(RepairOrder).where(
                        RepairOrder.dealer_id == ST_CHARLES,
                        RepairOrder.status == "READY_TO_DISPATCH",
                    ).order_by(RepairOrder.written_at.desc())
                )
            ).scalars()
        )
        ro_iter = iter(ros)

        def next_ro():
            return next(ro_iter, None)

        base = day_start + timedelta(hours=7)   # 7:00 local, in UTC
        made = 0
        colored = {"C": 0, "P": 0, "Q": 0}
        seeded_techs: set = set()

        # Give EVERY on-shift tech a full, working day so the board reads busy
        # (leave a couple idle for the red money-signal). RO budget: completed
        # blocks show HOURS (not an RO#), so they can safely reuse the pool; the
        # in-progress / queued blocks show the RO#, so those take DISTINCT ROs.
        to_seed = free_techs[: max(0, len(free_techs) - LEAVE_IDLE)]
        n_ros = len(ros) or 1
        _di = 0

        def take_distinct():
            nonlocal _di
            r = ros[_di] if _di < len(ros) else None
            _di += 1
            return r

        # each tech's day: a few completed (blue), one in-progress (green, running
        # now), and — for a rotating subset — a queued-next (light-green). Sorted
        # C -> P -> Q so the day tiles naturally from shift start.
        def plan_for(i: int) -> list[str]:
            n_completed = 2 if i % 5 == 0 else 3
            plan = ["C"] * n_completed + ["P"]
            if i % 2 == 0:
                plan.append("Q")
            return plan

        for ti, tech in enumerate(to_seed):
            skill, wants_comeback, match_base = _profile(ti)   # this tech's report profile
            comeback_used = False
            jobs = plan_for(ti)   # already C…, P, Q order
            offset = 0.0
            reuse_k = 0
            for job in jobs:
                if job == "C":
                    ro = ros[(ti * 4 + reuse_k) % n_ros]   # reuse ok (hours label)
                    reuse_k += 1
                else:
                    ro = take_distinct() or ros[(ti + reuse_k) % n_ros]  # distinct RO#
                if ro is None:
                    break
                # completed jobs feed the Reports (efficiency = guide/actual), so
                # they need a real guide; give a 0-guide demo RO a plausible one.
                est = float(ro.est_hours or 0)
                if job == "C" and est <= 0:
                    est = round(0.8 + (ti % 5) * 0.35, 1)
                    ro.est_hours = est
                est = est or 1.2
                # match score tracks FIT (from _profile), so Match Payoff shows the
                # high- vs low-match gap from real rows without inverting comebacks.
                match = max(55, min(96, match_base + (ti % 3 - 1) * 3))
                a = Assignment(
                    dealer_id=ST_CHARLES,
                    ro_id=ro.id,
                    technician_id=tech.id,
                    assigned_at=base,
                    assigned_by=SENTINEL,
                    match_score=match,
                    recommended_rank=1 + (ti % 4),   # some picks below the top 2
                    was_ai_recommendation=True,
                )
                start = base + timedelta(hours=offset)
                # Board colors come from the ASSIGNMENT's completed_at/started_at,
                # NOT ro.status. Completed jobs get an ACTUAL time from the tech's
                # skill (efficiency = guide/actual), giving the reports a real spread.
                if job == "C":
                    actual = max(0.3, round(est / skill, 2))
                    a.started_at = start
                    a.completed_at = start + timedelta(hours=actual)
                    offset += actual
                    # inject one comeback for a coaching/rushing tech's job
                    if wants_comeback and not comeback_used:
                        comeback_used = True
                        db.add(ComebackPairRow(
                            dealer_id=ST_CHARLES,
                            vin=ro.vin or "DEMOVIN",
                            concern_category=ro.concern_category or "Service",
                            original_ro_number=ro.ro_number,
                            original_closed_at=a.completed_at,
                            original_tech_id=tech.id,
                            repeat_ro_number=f"{CB_PREFIX}{ro.ro_number}",
                            repeat_opened_at=a.completed_at + timedelta(days=3),
                            days_between=3,
                        ))
                elif job == "P":
                    a.started_at = now - timedelta(minutes=40)   # running right now
                # Q — queued: started_at stays null; that's how the board reads it.
                colored[job] += 1
                db.add(a)
                made += 1
                seeded_techs.add(tech.id)

        await db.commit()
        print(
            f"Seeded {made} assignments across {len(seeded_techs)} techs "
            f"on {day.isoformat()} — completed={colored['C']} in_progress={colored['P']} "
            f"queued={colored['Q']}."
        )
        print(f"(free on-shift techs available: {len(free_techs)}; ready ROs used from {len(ros)})")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main("--clear" in sys.argv))
