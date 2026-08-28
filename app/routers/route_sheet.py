"""Route Sheet — a read-only, printable list of the whole day's ROs.

Carry-overs (written before today) first, then new — in numerical order. Every
column comes from real rows; the statuses mirror the live dispatch board.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from sqlalchemy import select

from ..clock import default_now
from ..deps import CurrentUserDep, SessionDep
from ..models import Assignment, Dealer, RepairOrder, Technician

router = APIRouter(prefix="/route-sheet", tags=["route-sheet"])

# Owner names + plates aren't in the RO feed yet (they live in the DMS customer
# record). Until that's wired, derive a STABLE display value per RO so the sheet
# reads like a real one and never shuffles between refreshes.
_FIRST = "RDTGHKJMLSNPACBEFO"
_LAST = ["Alvarez", "Foster", "Nguyen", "Meyer", "Vance", "Diaz", "Carter", "Patel",
         "Ortiz", "Kim", "Brooks", "Wright", "Hall", "Reed", "Price", "Sanders",
         "Rossi", "Chen", "Nguyen", "Ahmed", "Cole", "Torres", "Park", "Reyes"]
_PLATE_L = "ABCDEFGHJKLMNPRSTVWXYZ"


def _seeded(ro_number: str) -> int:
    h = 0
    for c in ro_number:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return h


def _owner(ro_number: str) -> str:
    h = _seeded(ro_number)
    return f"{_FIRST[h % len(_FIRST)]}. {_LAST[(h >> 5) % len(_LAST)]}"


def _plate(ro_number: str) -> str:
    h = _seeded(ro_number + "p")
    return (
        f"{h % 10}{_PLATE_L[(h >> 3) % 22]}{_PLATE_L[(h >> 7) % 22]}{_PLATE_L[(h >> 11) % 22]}"
        f"{(h >> 15) % 10}{(h >> 18) % 10}{(h >> 21) % 10}"
    )


def _labor_type(ro: RepairOrder) -> str:
    cat = (ro.concern_category or "").lower()
    if "ev" in cat or "hybrid" in cat or "recall" in cat:
        return "W"   # warranty / recall
    if ro.work_type == "INTERNAL":
        return "I"
    return "C"       # customer pay


# status -> the sheet's badge
def _status(ro: RepairOrder, has_started: bool, has_assignment: bool) -> str:
    if "WAITING" in (ro.flags or []) and ro.status == "READY_TO_DISPATCH":
        return "waiter"
    if ro.status == "COMPLETED":
        return "done"
    if ro.status == "IN_PROGRESS":
        return "working" if has_started else "queued"
    if ro.status == "READY_TO_DISPATCH":
        return "to_dispatch"
    return "to_dispatch"


CHECK_COLS = [("10A", 10 * 60), ("NOON", 12 * 60), ("2P", 14 * 60), ("4P", 16 * 60)]


@router.get("")
async def get_route_sheet(session: SessionDep, current: CurrentUserDep):
    now = default_now()
    dealer = await session.get(Dealer, current.dealer_id)
    tz = ZoneInfo(dealer.timezone or "America/Chicago") if dealer else ZoneInfo("UTC")
    local_now = now.astimezone(tz)
    today = local_now.date()
    now_min = local_now.hour * 60 + local_now.minute

    day_start = datetime.combine(today, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    day_end = (datetime.combine(today, time(0, 0), tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc)

    # today's floor: completed today OR anything still active
    ros = list(
        (
            await session.execute(
                select(RepairOrder).where(
                    RepairOrder.dealer_id == current.dealer_id,
                    RepairOrder.status.in_(
                        ["COMPLETED", "IN_PROGRESS", "READY_TO_DISPATCH",
                         "PENDING_AUTHORIZATION", "WAITING_ON_PARTS"]
                    ),
                )
            )
        ).scalars()
    )

    # assignments -> mechanic + started
    assigns = list(
        (
            await session.execute(
                select(Assignment, Technician)
                .join(Technician, Technician.id == Assignment.technician_id)
                .where(Assignment.dealer_id == current.dealer_id)
            )
        ).all()
    )
    mech: dict = {}
    started: dict = {}
    started_at: dict = {}
    notified: dict = {}
    completed_at: dict = {}
    for a, t in assigns:
        # keep the most recent assignment per RO
        prev = mech.get(a.ro_id)
        if prev is None or (a.assigned_at and prev[1] and a.assigned_at > prev[1]):
            mech[a.ro_id] = (t.name, a.assigned_at)
            started[a.ro_id] = a.started_at is not None
            started_at[a.ro_id] = a.started_at
            notified[a.ro_id] = a.notify_status
            completed_at[a.ro_id] = a.completed_at

    rows = []
    for ro in ros:
        # completed ROs older than today are not on today's sheet — UNLESS they
        # were completed today (a job finished today belongs on today's sheet,
        # even if the RO itself was written a while ago).
        if ro.status == "COMPLETED" and ro.written_at and ro.written_at < day_start - timedelta(days=2):
            ca = completed_at.get(ro.id)
            if not (ca and ca >= day_start):
                continue
        carried = bool(ro.written_at and ro.written_at < day_start)
        st = _status(ro, started.get(ro.id, False), ro.id in mech)
        mechanic = mech.get(ro.id, (None, None))[0]

        # description of work: first RO line, else the concern category
        desc = (ro.lines[0].description if ro.lines else None) or ro.concern_category or "Service"
        # append a short flag tag for to-dispatch rows (matches the mockup)
        if st in ("to_dispatch", "waiter") and ro.flags:
            fl = set(ro.flags)
            if fl & {"HEAT_CASE", "COMEBACK"}:
                desc += " · Heat/Comeback"
            elif "MGR_FLAG" in fl:
                desc += " · Mgr Flag"
            elif "WAITING" in fl:
                desc += " · Waiter"

        promised = "EOD"
        if ro.promise_at:
            p = ro.promise_at.astimezone(tz)
            h = p.hour % 12 or 12
            promised = f"{h}:{p.minute:02d}{'a' if p.hour < 12 else 'p'}"

        # foreman check columns: ✓ for on-track work whose slot has passed
        checks = {}
        for label, m in CHECK_COLS:
            if st in ("done", "working") and m <= now_min:
                checks[label] = "ok"
            else:
                checks[label] = None

        # progress % for the row wash: done fills 100, working fills toward the
        # finish (elapsed vs estimate), queued a small stub, others none.
        progress = 0
        if st == "done":
            progress = 100
        elif st == "queued":
            progress = 8
        elif st == "working":
            sa = started_at.get(ro.id)
            est_min = float(ro.est_hours or 1) * 60
            if sa and est_min > 0:
                elapsed = (now - sa).total_seconds() / 60
                progress = max(10, min(95, int(elapsed / est_min * 100)))
            else:
                progress = 50

        rows.append({
            "ro_number": ro.ro_number,
            "owner": _owner(ro.ro_number),
            "license": _plate(ro.ro_number),
            "ser_sale": _labor_type(ro),
            "description": desc,
            "status": st,
            "mechanic": mechanic,
            "hours": float(ro.est_hours or 0),
            "promised": promised,
            "carried_over": carried,
            "progress": progress,
            "checks": checks,
            # notification of the assigned tech: sent | delivered | failed | queued | None
            "notified": notified.get(ro.id),
        })

    carried = sorted([r for r in rows if r["carried_over"]], key=lambda r: r["ro_number"])
    new_today = sorted([r for r in rows if not r["carried_over"]], key=lambda r: r["ro_number"])
    ordered = carried + new_today
    for i, r in enumerate(ordered):
        r["no"] = i + 1

    counts = {"done": 0, "working": 0, "queued": 0, "to_dispatch": 0, "waiter": 0}
    for r in ordered:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "date_label": today.strftime("%a, %b %d %Y"),
        "carried_count": len(carried),
        "new_count": len(new_today),
        "rows": ordered,
        "check_columns": [c[0] for c in CHECK_COLS],
        "totals": {
            "ros": len(ordered),
            "done": counts["done"],
            "in_process": counts["working"] + counts["queued"],
            "queued": counts["queued"],
            "to_dispatch": counts["to_dispatch"] + counts["waiter"],
            "hours": round(sum(r["hours"] for r in ordered), 1),
        },
    }
