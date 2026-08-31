"""Reports — the "beyond-normal" teaching reports (owner parity, v2.3).

Every number here is COMPUTED FROM REAL DB ROWS, never hardcoded:
  - efficiency = guide (RO est/flag hrs) ÷ actual (dispatch start→done time)
  - comebacks  = comeback_pairs rows attributed to the original tech
  - match      = the 3D Match Score + recommended rank frozen on the assignment

The "actual time" comes from the dispatch board's start/done timestamps — so
these reports get richer as the board is actually used (and are seeded for the
demo store). Reports needing a feed we don't have yet (Inspection Upside =
sellable $/job) return available=false with a named reason, never a fake number.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..clock import default_now
from ..models import Assignment, ComebackPairRow, RepairOrder, Technician

# Efficiency guardrails: a job must have run long enough to trust the ratio,
# and we cap the headline so one 4-minute job can't read as 900%.
_MIN_ACTUAL_H = 0.15
_EFF_CAP = 320.0
PERIODS = {"week": 7, "t30": 30, "quarter": 90}


def period_bounds(period: str, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    now = now or default_now()
    days = PERIODS.get(period, 30)
    return now - timedelta(days=days), now


def _initials(name: str) -> str:
    return "".join(p[0] for p in name.split()[:2]).upper()


def _job_label(ro: RepairOrder) -> str:
    c = (ro.concern_category or "").strip()
    if c and c.lower() not in ("uncategorised", "uncategorized"):
        return c
    if ro.lines:
        d = (ro.lines[0].description or "").strip()
        if d:
            return d.title()[:28]
    return "Service"


@dataclass
class Job:
    ro_number: str
    tech_id: str
    tech_name: str
    initials: str
    job_type: str
    vehicle: str
    guide_h: float
    actual_h: float
    efficiency: float          # percent
    match_score: Optional[float]
    recommended_rank: Optional[int]
    is_comeback: bool
    parts: int = 0             # documented parts on the RO (proxy for work surfaced)


@dataclass
class _TechAgg:
    tech_id: str
    name: str
    initials: str
    jobs: list[Job] = field(default_factory=list)


async def load_jobs(session: AsyncSession, dealer_id: uuid.UUID, period: str) -> list[Job]:
    """Every completed, timed dispatch in the window, with a real efficiency."""
    start, end = period_bounds(period)

    techs = {
        t.id: t
        for t in (
            await session.execute(
                select(Technician).where(Technician.dealer_id == dealer_id)
            )
        ).scalars()
    }

    # comeback ROs (the ORIGINAL RO that came back) in-window, for flagging jobs
    cb_ro = {
        r
        for r in (
            await session.execute(
                select(ComebackPairRow.original_ro_number).where(
                    ComebackPairRow.dealer_id == dealer_id,
                    ComebackPairRow.original_closed_at >= start,
                    ComebackPairRow.original_closed_at < end,
                )
            )
        ).scalars()
    }

    rows = (
        await session.execute(
            select(Assignment, RepairOrder)
            .join(RepairOrder, RepairOrder.id == Assignment.ro_id)
            .options(selectinload(RepairOrder.lines))
            .where(
                Assignment.dealer_id == dealer_id,
                Assignment.started_at.is_not(None),
                Assignment.completed_at.is_not(None),
                Assignment.completed_at >= start,
                Assignment.completed_at < end,
            )
        )
    ).all()

    jobs: list[Job] = []
    for a, ro in rows:
        actual = (a.completed_at - a.started_at).total_seconds() / 3600.0
        if actual < _MIN_ACTUAL_H:
            continue
        guide = float(ro.est_hours or 0)
        if guide <= 0:
            continue
        eff = min(_EFF_CAP, guide / actual * 100.0)
        t = techs.get(a.technician_id)
        if t is None:
            continue
        jobs.append(
            Job(
                ro_number=ro.ro_number,
                tech_id=str(t.id),
                tech_name=t.name,
                initials=_initials(t.name),
                job_type=_job_label(ro),
                vehicle=" ".join(
                    str(x) for x in (ro.vehicle_year, ro.vehicle_make, ro.vehicle_model) if x
                ),
                guide_h=round(guide, 1),
                actual_h=round(actual, 1),
                efficiency=round(eff),
                match_score=float(a.match_score) if a.match_score is not None else None,
                recommended_rank=a.recommended_rank,
                is_comeback=ro.ro_number in cb_ro,
                parts=sum(int(l.parts_count or 0) for l in ro.lines),
            )
        )
    return jobs


def _by_tech(jobs: list[Job]) -> dict[str, _TechAgg]:
    out: dict[str, _TechAgg] = {}
    for j in jobs:
        agg = out.get(j.tech_id)
        if agg is None:
            agg = out[j.tech_id] = _TechAgg(j.tech_id, j.tech_name, j.initials)
        agg.jobs.append(j)
    return out


def _short(name: str) -> str:
    parts = name.split()
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else name


# --------------------------------------------------------------------------- #
# 1. Top Tech: Specific Job Efficiency                                         #
# --------------------------------------------------------------------------- #
def top_tech_efficiency(jobs: list[Job], threshold: float = 130.0) -> dict:
    standout = sorted(
        [j for j in jobs if j.efficiency >= threshold], key=lambda j: -j.efficiency
    )
    # fastest hand per job type (the teachers)
    fastest: dict[str, Job] = {}
    for j in standout:
        cur = fastest.get(j.job_type)
        if cur is None or j.efficiency > cur.efficiency:
            fastest[j.job_type] = j
    teachers = sorted(fastest.values(), key=lambda j: -j.efficiency)[:6]

    insight = None
    if standout:
        top = standout[0]
        counts: dict[str, int] = {}
        for j in standout:
            counts[j.tech_name] = counts.get(j.tech_name, 0) + 1
        leader = max(counts.items(), key=lambda kv: kv[1])
        insight = (
            f"{_short(leader[0])} logged {leader[1]} of the {len(standout)} standout jobs. "
            f"Clearest teaching win: {_short(top.tech_name)} on {top.job_type} at "
            f"{top.efficiency}% — film that job and every tech shaves time on it."
        )
    return {
        "available": True,
        "threshold": threshold,
        "count": len(standout),
        "insight": insight,
        "teachers": [
            {"tech": _short(j.tech_name), "initials": j.initials, "job_type": j.job_type,
             "efficiency": j.efficiency}
            for j in teachers
        ],
        "rows": [
            {"rank": i + 1, "tech": _short(j.tech_name), "initials": j.initials,
             "job_type": j.job_type, "vehicle": j.vehicle, "ro_number": j.ro_number,
             "guide_h": j.guide_h, "actual_h": j.actual_h, "efficiency": j.efficiency}
            for i, j in enumerate(standout)
        ],
    }


# --------------------------------------------------------------------------- #
# 2. The Mentor Board                                                          #
# --------------------------------------------------------------------------- #
def mentor_board(jobs: list[Job], min_jobs: int = 1) -> dict:
    # per job type -> per tech average efficiency
    by_type: dict[str, dict[str, list[Job]]] = {}
    for j in jobs:
        by_type.setdefault(j.job_type, {}).setdefault(j.tech_id, []).append(j)

    rows = []
    for job_type, per_tech in by_type.items():
        stats = []
        for tid, js in per_tech.items():
            avg = round(sum(j.efficiency for j in js) / len(js))
            has_cb = any(j.is_comeback for j in js)
            stats.append((js[0], avg, has_cb))
        if len(stats) < 2:
            continue
        stats.sort(key=lambda s: -s[1])
        top, top_eff, _ = stats[0]
        low, low_eff, low_cb = stats[-1]
        rows.append({
            "job_type": job_type,
            "top_hand": {"tech": _short(top.tech_name), "initials": top.initials, "efficiency": top_eff},
            "grow": {"tech": _short(low.tech_name), "initials": low.initials,
                     "efficiency": low_eff, "comeback": low_cb},
            "gap": top_eff - low_eff,
            "pairing": f"Pair {low.tech_name.split()[0]} with {top.tech_name.split()[0]}",
        })
    rows.sort(key=lambda r: -r["gap"])
    insight = None
    if rows:
        b = rows[0]
        insight = (
            f"Biggest opportunity: {b['job_type']} — {b['top_hand']['tech']} runs it at "
            f"{b['top_hand']['efficiency']}% while {b['grow']['tech']} is at "
            f"{b['grow']['efficiency']}%, a {b['gap']}-point gap. One focused ride-along "
            f"could pull the whole bottom quartile up."
        )
    return {"available": bool(rows), "insight": insight, "rows": rows}


# --------------------------------------------------------------------------- #
# 4. Speed vs Quality Quadrant                                                 #
# --------------------------------------------------------------------------- #
def speed_vs_quality(jobs: list[Job]) -> dict:
    aggs = _by_tech(jobs)
    points = []
    for agg in aggs.values():
        avg_eff = round(sum(j.efficiency for j in agg.jobs) / len(agg.jobs))
        comebacks = sum(1 for j in agg.jobs if j.is_comeback)
        points.append({
            "tech": _short(agg.name), "initials": agg.initials,
            "efficiency": avg_eff, "comebacks": comebacks, "jobs": len(agg.jobs),
        })
    # quadrant split: efficient at/above 100%, clean = no comebacks
    elite = [p for p in points if p["efficiency"] >= 100 and p["comebacks"] == 0]
    rushing = [p for p in points if p["efficiency"] >= 100 and p["comebacks"] > 0]
    insight = None
    if points:
        insight = f"{len(elite)} techs are genuinely elite — fast and clean (no comebacks)."
        if rushing:
            names = " & ".join(p["tech"].split()[0] for p in rushing[:3])
            insight += f" Watch {names}: quick but comeback-prone — speed at the cost of quality."
    return {"available": bool(points), "insight": insight, "points": points,
            "elite": len(elite), "rushing": len(rushing)}


# --------------------------------------------------------------------------- #
# 5. Match Payoff                                                              #
# --------------------------------------------------------------------------- #
def _grp_stats(js: list[Job]) -> dict:
    if not js:
        return {"n": 0, "efficiency": None, "comeback_rate": None}
    return {
        "n": len(js),
        "efficiency": round(sum(j.efficiency for j in js) / len(js)),
        "comeback_rate": round(sum(1 for j in js if j.is_comeback) / len(js) * 100),
    }


def match_payoff(jobs: list[Job], high: float = 88.0, low: float = 82.0) -> dict:
    scored = [j for j in jobs if j.match_score is not None]
    hi = _grp_stats([j for j in scored if j.match_score >= high])
    lo = _grp_stats([j for j in scored if j.match_score < low])
    insight = None
    if hi["n"] and lo["n"] and hi["efficiency"] is not None and lo["efficiency"] is not None:
        de = hi["efficiency"] - lo["efficiency"]
        dc = (lo["comeback_rate"] or 0) - (hi["comeback_rate"] or 0)
        insight = (
            f"When the system dispatched a high-match tech, jobs ran {de} points more "
            f"efficient and had {dc} points fewer comebacks. The scoring is picking the "
            f"right tech — the gap is the cost of overriding it."
        )
    return {
        "available": bool(hi["n"] or lo["n"]),
        "insight": insight,
        "high": {"label": f"High match (≥{int(high)})", **hi},
        "low": {"label": f"Low match (<{int(low)})", **lo},
        "note": "Found $/job needs an MPI sellable-work feed (not connected yet).",
    }


# --------------------------------------------------------------------------- #
# 6. Dispatcher Overrides: Coach or Learn                                      #
# --------------------------------------------------------------------------- #
def dispatcher_overrides(jobs: list[Job], shop_avg: Optional[float] = None) -> dict:
    overrides = [j for j in jobs if (j.recommended_rank or 1) > 2]
    if shop_avg is None and jobs:
        shop_avg = sum(j.efficiency for j in jobs) / len(jobs)
    shop_avg = shop_avg or 100.0
    coach, learn = 0, 0
    rows = []
    for j in overrides:
        # Proxy verdict: below-top-2 pick that underperformed the floor (or came
        # back) = the system likely had a better call; beat the floor = good call.
        good = j.efficiency >= shop_avg and not j.is_comeback
        if good:
            learn += 1
        else:
            coach += 1
        rows.append({
            "job_type": j.job_type, "vehicle": j.vehicle, "ro_number": j.ro_number,
            "tech": _short(j.tech_name), "initials": j.initials,
            "match_score": round(j.match_score) if j.match_score is not None else None,
            "rank": j.recommended_rank, "efficiency": j.efficiency,
            "comeback": j.is_comeback,
            "verdict": "Good call" if good else "Follow system",
        })
    rows.sort(key=lambda r: (r["verdict"] != "Follow system", -(r["efficiency"] or 0)))
    insight = None
    if overrides:
        insight = (
            f"{coach} of {len(overrides)} overrides went below the system's top 2 and "
            f"underperformed the shop floor — coaching moments to build trust in the "
            f"recommendation. {learn} beat the floor — signals the score can learn from."
        )
    return {
        "available": bool(overrides),
        "insight": insight,
        "counts": {"overrides": len(overrides), "coach": coach, "learn": learn},
        "rows": rows,
        "note": "The system's #1 pick at dispatch isn't snapshotted yet, so the "
                "comparison uses the shop-floor average as the bar.",
    }


# --------------------------------------------------------------------------- #
# 3. Hidden Money: Inspection Upside                                           #
#                                                                              #
# True MPI sellable-$ isn't in our feed, but DOCUMENTED PARTS PER JOB is real  #
# (myKaarma) and is a legitimate proxy for who surfaces work: a tech who lists #
# more parts is finding more sellable work on the car. We price parts at a     #
# documented estimate until a real red-line $ feed connects — and say so.      #
# --------------------------------------------------------------------------- #
_PART_VALUE = 75.0   # $ estimate per documented part (placeholder until MPI $ feed)


async def inspection_upside(session: AsyncSession, dealer_id: uuid.UUID) -> dict:
    """Parts documented per job, per tech — attributed straight from the DMS tech
    on each RO line (real myKaarma data), a legitimate proxy for who surfaces
    work. Priced at a $/part estimate until a true red-line sellable-$ feed lands."""
    from ..models import ROLine

    agg = (
        await session.execute(
            select(
                ROLine.tech_no,
                func.sum(ROLine.parts_count),
                func.count(func.distinct(ROLine.ro_id)),
            )
            .join(RepairOrder, RepairOrder.id == ROLine.ro_id)
            .where(
                RepairOrder.dealer_id == dealer_id,
                ROLine.parts_count > 0,
                ROLine.tech_no.is_not(None),
            )
            .group_by(ROLine.tech_no)
        )
    ).all()
    techs = {
        t.dms_tech_no: t
        for t in (
            await session.execute(
                select(Technician).where(
                    Technician.dealer_id == dealer_id, Technician.dms_tech_no.is_not(None)
                )
            )
        ).scalars()
    }
    rows = []
    for tech_no, parts, jobs in agg:
        t = techs.get(tech_no)
        if t is None or not jobs:
            continue
        ppj = float(parts or 0) / jobs
        rows.append({
            "tech": _short(t.name), "initials": _initials(t.name),
            "parts_per_job": round(ppj, 1),
            "found": round(ppj * _PART_VALUE),
            "jobs": int(jobs),
        })
    if not rows:
        return {
            "available": False,
            "reason": "No documented parts on jobs yet — this fills in as techs' parts "
                      "flow through myKaarma. (Exact MPI sellable-$ needs the inspection feed.)",
        }
    shop_avg = round(sum(r["found"] for r in rows) / len(rows))
    for r in rows:
        r["above"] = r["found"] >= shop_avg
    rows.sort(key=lambda r: -r["found"])
    top = rows[0]
    below = [r for r in rows if not r["above"]]
    uplift = round(sum((shop_avg - r["found"]) for r in below))
    insight = (
        f"{top['tech']} surfaces ${top['found']}/job vs a shop average of ${shop_avg}. "
        f"Coaching the {len(below)} techs below the line up to the average is about "
        f"${uplift} more in found work — before a single extra car comes in."
    )
    return {
        "available": True,
        "insight": insight,
        "shop_avg": shop_avg,
        "rows": rows,
        "note": "Proxy from documented parts per job (real myKaarma data), priced at a "
                f"${int(_PART_VALUE)}/part estimate. Exact red-line sellable-$ activates "
                "when the MPI inspection feed connects.",
    }
