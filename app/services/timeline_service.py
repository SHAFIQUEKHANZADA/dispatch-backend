"""Dispatcher timeline — the Gantt board.

Builds, for one day, each technician's row of time blocks across their shift:
completed work, the in-progress RO, queued-next ROs, lunch, idle gaps, and
unallocated capacity. Every block is derived from REAL rows — the technician's
shift/lunch and their actual assignment rows — never mocked.

Layout is expressed in local minutes-from-midnight so the frontend can position
blocks as simple percentages across the day window.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import default_now
from ..models import Assignment, Dealer, RepairOrder, Technician

# block kinds — must match the frontend legend
COMPLETED = "completed"
IN_PROGRESS = "in_progress"
QUEUED = "queued"
IDLE_NEEDS = "idle_needs"       # idle now, needs work
IDLE_LOST = "idle_lost"         # idle gap between ROs (lost time)
LUNCH = "lunch"
UNALLOCATED = "unallocated"
OFF_SHIFT = "off_shift"

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Block:
    kind: str
    start_min: int          # minutes from local midnight
    end_min: int
    label: str = ""
    ro_number: Optional[str] = None
    hours: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "start_min": self.start_min,
            "end_min": self.end_min,
            "label": self.label,
            "ro_number": self.ro_number,
            "hours": round(self.hours, 1) if self.hours is not None else None,
        }


@dataclass
class TechRow:
    id: str
    name: str
    initials: str
    level: str
    team: str
    shift_start_min: Optional[int]
    shift_end_min: Optional[int]
    on_shift: bool
    status_chip: Optional[dict] = None      # {"kind","text"} or None
    blocks: list[Block] = field(default_factory=list)
    base_hours: float = 0.0          # flagged hours dispatched today
    up_hours: float = 0.0            # gained vs base (over-performance)
    down_hours: float = 0.0          # lost (idle) hours
    attention: int = 0               # sort key: higher = needs attention

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "initials": self.initials,
            "level": self.level,
            "team": self.team,
            "shift_start_min": self.shift_start_min,
            "shift_end_min": self.shift_end_min,
            "on_shift": self.on_shift,
            "status_chip": self.status_chip,
            "blocks": [b.to_dict() for b in self.blocks],
            "totals": {
                "base": round(self.base_hours, 1),
                "up": round(self.up_hours, 1),
                "down": round(self.down_hours, 1),
            },
        }


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return ("".join(p[0] for p in parts[:2])).upper() or "?"


def _to_min(t: Optional[time]) -> Optional[int]:
    return None if t is None else t.hour * 60 + t.minute


# Map our skill_level values to the dispatcher board's label style.
_LEVEL_MAP = {
    "Apprentice 1": "L1 Apprentice",
    "Apprentice 2": "L2 Apprentice",
    "Apprentice 3": "L3 Apprentice",
    "General Tech": "General Tech",
    "Diagnostic Tech": "Diagnostic Tech",
    "Master": "Master",
    "Sr. Master": "Senior Master",
}


def _level_label(skill: Optional[str], certs: list[str]) -> str:
    base = _LEVEL_MAP.get(skill or "", skill or "Tech")
    if any(c in certs for c in ("HV_EV", "HYBRID")):
        base = f"{base} · HV"
    return base


async def build_timeline(
    session: AsyncSession,
    dealer_id: uuid.UUID,
    *,
    on: Optional[date] = None,
    now: Optional[datetime] = None,
    include_off: bool = False,
) -> dict:
    now = now or default_now()
    dealer = await session.get(Dealer, dealer_id)
    tz = ZoneInfo(dealer.timezone or "America/Chicago") if dealer else ZoneInfo("UTC")
    local_now = now.astimezone(tz)
    day = on or local_now.date()
    day_name = _DAY_NAMES[day.weekday()]
    now_min = local_now.hour * 60 + local_now.minute if day == local_now.date() else None

    # window: 7a–7p by default, widened to cover any shift
    view_start, view_end = 7 * 60, 19 * 60

    techs = list(
        (
            await session.execute(
                select(Technician).where(Technician.dealer_id == dealer_id)
            )
        ).scalars()
    )

    # today's assignments for these techs, with their RO
    day_start = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    day_end = (datetime.combine(day, time(0, 0), tzinfo=tz) + timedelta(days=1)).astimezone(
        timezone.utc
    )
    rows = list(
        (
            await session.execute(
                select(Assignment, RepairOrder)
                .join(RepairOrder, RepairOrder.id == Assignment.ro_id)
                .where(
                    Assignment.dealer_id == dealer_id,
                    Assignment.assigned_at >= day_start,
                    Assignment.assigned_at < day_end,
                )
            )
        ).all()
    )
    by_tech: dict[uuid.UUID, list[tuple[Assignment, RepairOrder]]] = {}
    for a, ro in rows:
        by_tech.setdefault(a.technician_id, []).append((a, ro))

    counters = {"idle": 0, "unplanned": 0, "back_soon": 0, "assigned": 0, "available": 0}

    tech_rows: list[TechRow] = []
    for t in techs:
        work_days = {d.strip()[:3].title() for d in (t.work_days or []) if d}
        on_shift = bool(work_days) and day_name in work_days and bool(t.shift_start and t.shift_end)
        if not on_shift and not include_off:
            continue

        certs = [c.cert_type for c in (t.certs or [])]
        ss, se = _to_min(t.shift_start), _to_min(t.shift_end)
        ls, le = _to_min(t.lunch_start), _to_min(t.lunch_end)
        view_start = min(view_start, ss if ss is not None else view_start)
        view_end = max(view_end, se if se is not None else view_end)

        row = TechRow(
            id=str(t.id),
            name=t.name,
            initials=_initials(t.name),
            level=_level_label(t.skill_level, certs),
            team=t.team or "Main",
            shift_start_min=ss,
            shift_end_min=se,
            on_shift=on_shift,
        )

        if not on_shift:
            if ss is not None and se is not None:
                row.blocks.append(Block(OFF_SHIFT, ss, se, "Off"))
            # off-shift techs still show a hatched off-shift bar across the view
            row.blocks = [Block(OFF_SHIFT, view_start, view_end, "")]
            tech_rows.append(row)
            continue

        # Order the work: completed (earliest first), then the in-progress RO,
        # then queued. This is a PLANNED tiling — the day laid out edge to edge.
        pairs = by_tech.get(t.id, [])
        completed = sorted(
            [(a, ro) for a, ro in pairs if a.completed_at is not None],
            key=lambda p: p[0].completed_at or now,
        )
        in_prog = [(a, ro) for a, ro in pairs if a.started_at is not None and a.completed_at is None]
        queued = [(a, ro) for a, ro in pairs if a.started_at is None]

        blocks: list[Block] = []
        cursor = ss if ss is not None else view_start
        base_h = 0.0
        lunch_placed = ls is None or le is None

        def maybe_lunch(before_end: int):
            """Drop the lunch block in when the packed work reaches it."""
            nonlocal cursor, lunch_placed
            if not lunch_placed and cursor < le and before_end > ls:
                # small idle gap between finishing work and lunch = lost time
                if cursor < ls:
                    blocks.append(Block(IDLE_LOST, cursor, ls, ""))
                blocks.append(Block(LUNCH, ls, le, ""))
                cursor = le
                lunch_placed = True

        def place(kind: str, dur: int, label: str, ro_number, hours: float):
            nonlocal cursor
            maybe_lunch(cursor + dur)
            start = cursor
            end = min(start + dur, se if se is not None else start + dur)
            blocks.append(Block(kind, start, end, label, ro_number=ro_number, hours=hours))
            cursor = end

        for a, ro in completed:
            dur = int(round(float(ro.est_hours or 0) * 60)) or 30
            hrs = float(ro.est_hours or 0)
            # efficiency gain: book (flagged) hours minus actual time on the job.
            # Shown as "1.0 +0.3" when the tech beat book time — same as his board.
            gain = 0.0
            if a.started_at and a.completed_at:
                actual = (a.completed_at - a.started_at).total_seconds() / 3600.0
                gain = round(hrs - actual, 1)
            base_txt = f"{hrs:.1f}".rstrip("0").rstrip(".")
            label = f"{base_txt} +{gain:.1f}".rstrip("0").rstrip(".") if gain >= 0.1 else base_txt
            place(COMPLETED, dur, label, ro.ro_number, hrs)
            base_h += hrs
            if gain > 0:
                row.up_hours += gain
            counters["assigned"] += 1

        for a, ro in in_prog:
            dur = int(round(float(ro.est_hours or 0) * 60)) or 30
            hrs = float(ro.est_hours or 0)
            place(IN_PROGRESS, dur, f"RO {ro.ro_number}", ro.ro_number, hrs)
            base_h += hrs
            counters["assigned"] += 1

        # if the tech is on the clock, has finished their work, and has nothing
        # running or queued -> idle, needs work (this is the money signal)
        idle_now = (
            now_min is not None
            and se is not None
            and cursor <= now_min < se
            and not in_prog
            and not queued
        )
        if idle_now:
            maybe_lunch(now_min)
            idle_end = min(se, now_min + 45)
            if cursor < idle_end:
                blocks.append(Block(IDLE_NEEDS, cursor, idle_end, "Idle"))
                row.down_hours += (idle_end - cursor) / 60.0
                cursor = idle_end
                counters["idle"] += 1

        for a, ro in queued:
            dur = int(round(float(ro.est_hours or 0) * 60)) or 30
            hrs = float(ro.est_hours or 0)
            place(QUEUED, dur, f"RO {ro.ro_number}", ro.ro_number, hrs)

        # lunch still not placed (short day) -> drop it if it fits the shift
        if not lunch_placed and se is not None and ls is not None and ls >= cursor and ls < se:
            if cursor < ls:
                blocks.append(Block(UNALLOCATED, cursor, ls, ""))
            blocks.append(Block(LUNCH, ls, le, ""))
            cursor = le
            lunch_placed = True

        # fill the rest of the shift with unallocated (open capacity)
        if se is not None and cursor < se:
            blocks.append(Block(UNALLOCATED, cursor, se, ""))
            cursor = se

        # after the shift ends -> off-shift hatch to the end of the view window
        if se is not None and se < view_end:
            blocks.append(Block(OFF_SHIFT, se, view_end, ""))

        blocks.sort(key=lambda b: b.start_min)
        row.blocks = blocks
        row.base_hours = base_h
        row.up_hours = round(row.up_hours, 1)   # accumulated from real efficiency gains

        # status chip
        if idle_now:
            mins = min(45, (se - now_min)) if se else 45
            row.status_chip = {"kind": "idle", "text": f"IDLE {mins}M"}
            row.attention += 300
        elif base_h == 0 and not in_prog and not queued:
            row.status_chip = {"kind": "no_plan", "text": "NO PLAN"}
            counters["unplanned"] += 1
            row.attention += 100
        elif any(
            b.kind == LUNCH and now_min is not None and b.start_min <= now_min < b.end_min
            for b in blocks
        ):
            back = next(b.end_min - now_min for b in blocks if b.kind == LUNCH)
            row.status_chip = {"kind": "lunch", "text": f"LUNCH · BACK {back}M"}

        if not in_prog and not queued and not idle_now:
            counters["available"] += 1

        tech_rows.append(row)

    # unassigned ROs (ready to dispatch)
    unassigned = (
        await session.execute(
            select(RepairOrder).where(
                RepairOrder.dealer_id == dealer_id,
                RepairOrder.status == "READY_TO_DISPATCH",
            )
        )
    ).scalars().all()

    # group by team, sorted by attention (needs-attention first)
    order = ["Main", "Lube", "Express", "Used", "Internal"]
    labels = {"Main": "MAIN SHOP", "Lube": "LUBE TEAM", "Express": "EXPRESS",
              "Used": "USED", "Internal": "INTERNAL"}
    groups = []
    for team in order:
        members = [r for r in tech_rows if (r.team or "Main") == team]
        if not members:
            continue
        members.sort(key=lambda r: (-r.attention, r.name))
        groups.append(
            {"team": team, "label": labels.get(team, team.upper()),
             "count": len(members), "techs": [r.to_dict() for r in members]}
        )

    return {
        "date": day.isoformat(),
        "day_name": local_now.strftime("%a"),
        "date_label": day.strftime("%a, %b %d %Y"),
        "view_start_min": view_start,
        "view_end_min": view_end,
        "now_min": now_min,
        "counters": {
            "idle": counters["idle"],
            "unplanned": counters["unplanned"],
            "back_soon": counters["back_soon"],
            "assigned": counters["assigned"],
            "available": counters["available"],
            "unassigned": len(unassigned),
        },
        "groups": groups,
        "legend": [
            {"kind": COMPLETED, "label": "Completed"},
            {"kind": IN_PROGRESS, "label": "In progress"},
            {"kind": QUEUED, "label": "Queued next"},
            {"kind": IDLE_NEEDS, "label": "Idle — needs work"},
            {"kind": IDLE_LOST, "label": "Idle between ROs (lost)"},
            {"kind": LUNCH, "label": "Lunch / break"},
            {"kind": UNALLOCATED, "label": "Unallocated"},
            {"kind": OFF_SHIFT, "label": "Off shift"},
        ],
    }
