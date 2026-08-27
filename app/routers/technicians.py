"""FR-1 (Technician Settings — the 60–90 min SM onboarding session)
and FR-5 (the Available Techs screen)."""

from __future__ import annotations

import re
import uuid
from datetime import date, time
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from .. import audit
from ..deps import CurrentUserDep, SessionDep
from ..models import (
    Technician,
    TechnicianCert,
    TechnicianRestriction,
    TechnicianSpecialty,
)
from ..services.dispatch_service import (
    load_shop,
    rank_for_ro,
    ro_to_input,
    techs_for_ro,
)
from ..engine.match_score import check_hard_constraints, score_technician
from ..models import Assignment, RepairOrder

router = APIRouter(prefix="/technicians", tags=["technicians"])

TEAMS = ["Main", "Lube", "Express", "Used", "Internal"]
SKILL_LEVELS = [
    "Apprentice 1", "Apprentice 2", "Apprentice 3", "General Tech",
    "Diagnostic Tech", "Master", "Sr. Master",
]
CERT_TYPES = [
    "OEM_HONDA_PACT", "ASE", "HV_EV", "HYBRID", "DIAGNOSTIC",
    "TRANSMISSION", "HVAC", "ALIGNMENT", "LUBE",
]
WORK_TYPES = [
    "ENGINE", "TRANSMISSION", "ELECTRICAL", "HVAC", "BRAKES",
    "SUSPENSION", "DIAGNOSTIC", "MAINTENANCE", "ALIGNMENT", "LUBE", "EV",
]
WORK_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class CertIn(BaseModel):
    cert_type: str
    level: Optional[str] = None
    expires_on: Optional[date] = None


class SpecialtyIn(BaseModel):
    work_type: Optional[str] = None
    vehicle_specialty: Optional[str] = None


# The DMS tech # is the join key to the dealership's DMS export — a typo here
# silently orphans every one of that tech's history rows (they import as
# "unmatched"), which quietly poisons their familiarity map and scoreboard.
# So it is validated, not trusted.
DMS_TECH_NO_RE = re.compile(r"^[A-Za-z]{0,3}\d{2,6}$")


class TechnicianIn(BaseModel):
    name: str
    employee_id: Optional[str] = None
    dms_tech_no: Optional[str] = None
    team: Optional[str] = None
    skill_level: Optional[str] = None
    active: bool = True

    @field_validator("dms_tech_no")
    @classmethod
    def valid_dms_no(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip().upper()
        if not DMS_TECH_NO_RE.match(v):
            raise ValueError(
                f"'{v}' is not a valid DMS tech number. Expected the format your DMS "
                f"uses — up to 3 letters followed by 2–6 digits (e.g. T104, H045, 231)."
            )
        return v

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Name is required")
        return v.strip()

    shift_start: Optional[time] = None
    shift_end: Optional[time] = None
    work_days: list[str] = Field(default_factory=list)
    lunch_start: Optional[time] = None
    lunch_end: Optional[time] = None

    max_daily_hours: Optional[float] = None
    overtime_threshold: Optional[float] = None
    efficiency_target: Optional[float] = None
    productivity_target: Optional[float] = None

    certs: list[CertIn] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    specialties: list[SpecialtyIn] = Field(default_factory=list)


# engine skill_level (a SKILL_RANKS key) -> the owner's roster role label
_ROLE_LABEL = {
    "Sr. Master": "Senior Master",
    "Master": "Master",
    "Diagnostic Tech": "L5 Apprentice",
    "General Tech": "L4 Apprentice",
    "Apprentice 3": "L3 Apprentice",
    "Apprentice 2": "L2 Apprentice",
    "Apprentice 1": "L1 Apprentice",
}
_TEAM_LABEL = {"Main": "Main Shop", "Lube": "Lube Team"}


def role_label(t: Technician) -> str:
    if t.team == "Lube":
        return "Lube Tech"
    return _ROLE_LABEL.get(t.skill_level or "", t.skill_level or "—")


def _tech_dict(t: Technician) -> dict:
    return {
        "id": str(t.id),
        "name": t.name,
        "employee_id": t.employee_id,
        "dms_tech_no": t.dms_tech_no,
        "team": t.team,
        "team_label": _TEAM_LABEL.get(t.team or "", t.team or "—"),
        "skill_level": t.skill_level,
        "role_label": role_label(t),
        "hourly_rate": float(t.hourly_rate) if t.hourly_rate is not None else None,
        "cert_badges": list(t.cert_badges or []),
        "bio_status": t.bio_status,
        "bio_reviewed_label": t.bio_reviewed_label,
        "bio_submitted_label": t.bio_submitted_label,
        "has_pending_bio": bool(t.pending_bio),
        "active": t.active,
        "shift_start": t.shift_start.isoformat() if t.shift_start else None,
        "shift_end": t.shift_end.isoformat() if t.shift_end else None,
        "work_days": list(t.work_days or []),
        "lunch_start": t.lunch_start.isoformat() if t.lunch_start else None,
        "lunch_end": t.lunch_end.isoformat() if t.lunch_end else None,
        "max_daily_hours": float(t.max_daily_hours) if t.max_daily_hours is not None else None,
        "overtime_threshold": float(t.overtime_threshold) if t.overtime_threshold is not None else None,
        "efficiency_target": float(t.efficiency_target) if t.efficiency_target is not None else None,
        "productivity_target": float(t.productivity_target) if t.productivity_target is not None else None,
        "certs": [
            {
                "cert_type": c.cert_type,
                "level": c.level,
                "expires_on": c.expires_on.isoformat() if c.expires_on else None,
            }
            for c in t.certs
        ],
        "restrictions": [r.blocked_work_type for r in t.restrictions],
        "specialties": [
            {"work_type": s.work_type, "vehicle_specialty": s.vehicle_specialty}
            for s in t.specialties
        ],
        # FR-1.6 — completeness indicator
        "missing_fields": t.missing_fields(),
        "completeness_pct": t.completeness_pct(),
    }


@router.get("/options")
async def options():
    """Everything the onboarding form's dropdowns need."""
    return {
        "teams": TEAMS,
        "skill_levels": SKILL_LEVELS,
        "cert_types": CERT_TYPES,
        "work_types": WORK_TYPES,
        "work_days": WORK_DAYS,
    }


@router.get("")
async def list_technicians(
    session: SessionDep, current: CurrentUserDep, include_inactive: bool = True
):
    q = select(Technician).where(Technician.dealer_id == current.dealer_id)
    if not include_inactive:
        q = q.where(Technician.active.is_(True))
    techs = list((await session.execute(q.order_by(Technician.name))).scalars())
    return {"technicians": [_tech_dict(t) for t in techs]}


_PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


@router.get("/recommendations")
async def tech_recommendations(session: SessionDep, current: CurrentUserDep):
    """Available-Techs board: for each free/soon-free tech, the Ready-to-Dispatch
    ROs ranked by THAT tech's Match Score (not just global priority).

    The inverse of the dispatch board. Same deterministic engine — we just pivot:
    score every eligible (tech, RO) pair, then group by tech.
    """
    shop = await load_shop(session, current.dealer_id)
    from datetime import datetime, timezone

    ready = list(
        (
            await session.execute(
                select(RepairOrder).where(
                    RepairOrder.dealer_id == current.dealer_id,
                    RepairOrder.status == "READY_TO_DISPATCH",
                )
            )
        ).scalars()
    )

    # global priority rank of each RO (flagged first, then priority, then written)
    far = datetime.max.replace(tzinfo=timezone.utc)
    ordered = sorted(
        ready,
        key=lambda r: (
            0 if r.is_flagged else 1,
            _PRIORITY_RANK.get(r.priority, 3),
            r.written_at or far,
            r.ro_number,
        ),
    )
    priority_rank = {r.id: i + 1 for i, r in enumerate(ordered)}

    # best-fit tech per RO (for the insight banner)
    best_tech_for_ro: dict = {}
    for ro in ready:
        rk = rank_for_ro(shop, ro, top_n=1)
        if rk.candidates:
            best_tech_for_ro[ro.id] = rk.candidates[0].name

    # score every eligible (tech, RO) pair, grouped by tech
    per_tech: dict = {t.id: [] for t in shop.technicians}
    for ro in ready:
        ro_in = ro_to_input(ro)
        ctx = shop.context(ro.concern_category)
        for tech_input in techs_for_ro(shop, ro):
            if check_hard_constraints(ro_in, tech_input, ctx) is not None:
                continue
            cand = score_technician(ro_in, tech_input, ctx)
            per_tech[uuid.UUID(tech_input.id)].append((ro, cand))

    out = []
    available_freeing = 0
    now = shop.now
    for t in shop.technicians:
        ti = shop.tech_inputs[t.id]
        if not (ti.on_shift and ti.active):
            continue

        # status
        assigned = shop.assigned_hours.get(t.id, 0.0)
        if ti.idle if hasattr(ti, "idle") else (assigned <= 0.01 and not shop.current_ro.get(t.id)):
            status = {"kind": "idle", "text": "IDLE"}
        elif ti.free_at and ti.free_at <= now:
            status = {"kind": "available", "text": "AVAILABLE NOW"}
        elif ti.free_at:
            mins = int(round((ti.free_at - now).total_seconds() / 60.0))
            status = {"kind": "freeing", "text": f"FREES IN ~{max(5, mins)} MIN"}
        else:
            status = {"kind": "available", "text": "AVAILABLE NOW"}
        if status["kind"] in ("available", "freeing", "idle"):
            available_freeing += 1

        recs = sorted(per_tech[t.id], key=lambda pair: -pair[1].score)[:3]
        ro_rows = []
        for ro, cand in recs:
            ro_rows.append({
                "ro_id": str(ro.id),
                "ro_number": ro.ro_number,
                "vehicle": " ".join(str(x) for x in (ro.vehicle_year, ro.vehicle_make, ro.vehicle_model) if x),
                "concern": ro.concern_category,
                "concern_short": (ro.lines[0].description if ro.lines else ro.concern_category),
                "priority_rank": priority_rank.get(ro.id),
                "score": cand.score,
                "reasons": [r.to_dict() for r in cand.reasons if r.text],
                "warnings": cand.warnings,
                "technician_id": str(t.id),
            })

        # insight banner — real observation about this tech's routing
        insight = None
        if ro_rows:
            top = recs[0][0]
            top_row = ro_rows[0]
            # is there a higher-priority RO that went to a better-fit tech?
            higher = [r for r in ordered if priority_rank[r.id] < (top_row["priority_rank"] or 99)]
            stolen = next(
                (r for r in higher if best_tech_for_ro.get(r.id) and best_tech_for_ro[r.id] != t.name),
                None,
            )
            if stolen is not None:
                insight = (
                    f"#{priority_rank[stolen.id]} priority (RO #{stolen.ro_number} "
                    f"{stolen.concern_category}) is a better fit for {best_tech_for_ro[stolen.id]} — "
                    f"so {t.name.split()[0]} is matched to the {top.concern_category} they fit best."
                )
            else:
                insight = f"{t.name.split()[0]} is the best fit for RO #{top.ro_number} ({top.concern_category})."

        cert_badge = None
        if any(c in ti.certs for c in ("HV_EV", "HYBRID")):
            cert_badge = "HV"
        elif t.team == "Lube":
            cert_badge = "LUBE TEAM"

        out.append({
            "id": str(t.id),
            "name": t.name,
            "initials": "".join(p[0] for p in t.name.split()[:2]).upper(),
            "level": t.skill_level or "Tech",
            "status": status,
            "cert_badge": cert_badge,
            "insight": insight,
            "ros": ro_rows,
        })

    # available/freeing techs first
    out.sort(key=lambda x: (0 if x["status"]["kind"] in ("available", "freeing") else 1, x["name"]))

    return {
        "counters": {"available_freeing": available_freeing, "unassigned_ros": len(ready)},
        "techs": out,
    }


@router.get("/available")
async def available_technicians(session: SessionDep, current: CurrentUserDep):
    """FR-5 — who is on shift, what they are holding, who is idle."""
    shop = await load_shop(session, current.dealer_id)

    out = []
    for t in shop.technicians:
        ti = shop.tech_inputs[t.id]
        assigned = shop.assigned_hours.get(t.id, 0.0)
        capacity = float(t.max_daily_hours or 8.0)
        ro = shop.current_ro.get(t.id)

        # Today's in-category efficiency is not a thing; today's efficiency is
        # the store-wide one from the familiarity map, which is a 90-day figure.
        # Rather than mislabel it, we surface the 90-day category spread.
        fam = [f for (tid, _), f in shop.familiarity.items() if tid == t.id]
        total_flagged = sum(float(f.flagged_hours or 0) for f in fam)
        total_clocked = sum(float(f.clocked_hours or 0) for f in fam)
        eff_t90 = (total_flagged / total_clocked * 100.0) if total_clocked > 0 else None

        idle = ti.on_shift and ti.active and assigned <= 0.01

        out.append(
            {
                "id": str(t.id),
                "name": t.name,
                "team": t.team,
                "skill_level": t.skill_level,
                "level_label": ti.level_label,
                "on_shift": ti.on_shift,
                "active": ti.active,
                "idle": idle,
                "overloaded": assigned > capacity,
                "assigned_hours": round(assigned, 1),
                "capacity_hours": capacity,
                "overtime_threshold": float(t.overtime_threshold or capacity),
                "free_at": ti.free_at.isoformat() if ti.free_at else None,
                "shift_start": t.shift_start.isoformat() if t.shift_start else None,
                "shift_end": t.shift_end.isoformat() if t.shift_end else None,
                "certs": list(ti.certs),
                "current_ro": (
                    {
                        "id": str(ro.id),
                        "ro_number": ro.ro_number,
                        "concern_category": ro.concern_category,
                        "est_hours": float(ro.est_hours or 0),
                    }
                    if ro
                    else None
                ),
                "efficiency_t90": round(eff_t90, 1) if eff_t90 is not None else None,
                "efficiency_target": (
                    float(t.efficiency_target) if t.efficiency_target is not None else None
                ),
                # Guardian: say why a number is missing rather than showing a dash.
                "data_issues": list(ti.data_issues),
            }
        )

    return {"technicians": out}


# --------------------------------------------------------------------------- #
# Bio-approval workflow (Tech Settings)                                        #
# NOTE: declared BEFORE /{tech_id} so "bio-updates" isn't parsed as a UUID.    #
# --------------------------------------------------------------------------- #

@router.get("/bio-updates")
async def bio_updates(session: SessionDep, current: CurrentUserDep):
    """Techs who have submitted a bio update awaiting manager approval."""
    techs = list((await session.execute(
        select(Technician).where(
            Technician.dealer_id == current.dealer_id,
            Technician.bio_status == "pending",
            Technician.pending_bio.isnot(None),
        )
    )).scalars())
    techs.sort(key=lambda t: t.bio_submitted_label or "", reverse=True)  # most recent first
    updates = [
        {
            "id": str(t.id),
            "name": t.name,
            "role_label": role_label(t),
            "submitted_label": t.bio_submitted_label,
            "bio": t.pending_bio,
        }
        for t in techs
    ]
    return {"pending": len(updates), "updates": updates}


@router.post("/{tech_id}/bio/{action}")
async def act_on_bio(
    tech_id: uuid.UUID, action: str, session: SessionDep, current: CurrentUserDep
):
    """Approve / reject / request-changes on a pending bio update.

    Approve applies the submitted cert badges to the roster and marks the bio
    approved (so it flows into scoring); the others just clear the pending state
    with the recorded outcome.
    """
    current.require_role("SERVICE_MANAGER")
    if action not in {"approve", "reject", "request-changes"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown action")
    t = await session.get(Technician, tech_id)
    if t is None or t.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Technician not found")

    from ..clock import default_now
    reviewed = default_now().strftime("%b %Y")

    if action == "approve":
        bio = t.pending_bio or {}
        # fold any newly-attached ASE certs into the roster badges
        added = [a.get("code") for a in bio.get("ase_added", []) if a.get("code")]
        if added:
            badges = list(t.cert_badges or [])
            for code in added:
                tag = f"ASE {code}"
                if tag not in badges:
                    badges.append(tag)
            t.cert_badges = badges
        t.bio_status = "approved"
        t.bio_reviewed_label = reviewed
        t.pending_bio = None
    elif action == "reject":
        t.bio_status = "approved"        # reverts to the last approved bio
        t.bio_reviewed_label = reviewed
        t.pending_bio = None
    else:  # request-changes — bounce back to the tech, keep it out of the queue
        t.bio_status = "changes_requested"
        t.pending_bio = None

    await audit.record(
        session, dealer_id=current.dealer_id, actor=current.user_id,
        action=audit.SETTINGS_UPDATED, entity="technician_bio", entity_id=t.id,
        payload={"action": action, "name": t.name},
    )
    await session.commit()
    return {"id": str(t.id), "bio_status": t.bio_status, "action": action}


# --------------------------------------------------------------------------- #
# Tech-facing "My Work" view — the notification path we own end-to-end.        #
# A tech opens this on their phone/tablet; when the dispatcher assigns them,    #
# the job appears here (live), so an assignment reaches the tech without any    #
# DMS write-back. Declared before /{tech_id} to keep routing unambiguous.      #
# --------------------------------------------------------------------------- #

@router.get("/{tech_id}/my-work")
async def my_work(tech_id: uuid.UUID, session: SessionDep, current: CurrentUserDep):
    """A technician's own live worklist — jobs dispatched to them, not yet done."""
    t = await session.get(Technician, tech_id)
    if t is None or t.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Technician not found")

    rows = list(
        (
            await session.execute(
                select(Assignment, RepairOrder)
                .join(RepairOrder, RepairOrder.id == Assignment.ro_id)
                .where(
                    Assignment.technician_id == tech_id,
                    Assignment.completed_at.is_(None),
                )
                .order_by(Assignment.assigned_at.desc())
            )
        ).all()
    )

    jobs = []
    for a, ro in rows:
        jobs.append({
            "assignment_id": str(a.id),
            "ro_id": str(ro.id),
            "ro_number": ro.ro_number,
            "vehicle": " ".join(str(x) for x in (ro.vehicle_year, ro.vehicle_make, ro.vehicle_model) if x),
            "concern": ro.concern_category or "Service",
            "concern_short": (ro.lines[0].description if ro.lines else ro.concern_category) or "Service",
            "est_hours": float(ro.est_hours or 0),
            "promise_at": ro.promise_at.isoformat() if ro.promise_at else None,
            "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
            "state": "working" if a.started_at else "assigned",
        })
    return {
        "technician_id": str(t.id),
        "technician_name": t.name,
        "count": len(jobs),
        "jobs": jobs,
    }


@router.post("/assignment/{assignment_id}/{action}")
async def update_assignment(
    assignment_id: uuid.UUID, action: str, session: SessionDep, current: CurrentUserDep
):
    """Tech advances their own job from the My Work view: start | done."""
    if action not in {"start", "done"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown action")
    a = await session.get(Assignment, assignment_id)
    if a is None or a.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")

    from ..clock import default_now
    now = default_now()
    ro = await session.get(RepairOrder, a.ro_id)
    if action == "start":
        a.started_at = a.started_at or now
        if ro:
            ro.status = "IN_PROGRESS"
    else:  # done
        a.completed_at = now
        a.started_at = a.started_at or now
        if ro:
            ro.status = "COMPLETED"
    await session.commit()
    return {"assignment_id": str(a.id), "action": action, "ok": True}


@router.get("/{tech_id}")
async def get_technician(tech_id: uuid.UUID, session: SessionDep, current: CurrentUserDep):
    t = await session.get(Technician, tech_id)
    if t is None or t.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Technician not found")
    return _tech_dict(t)


async def _apply(session, t: Technician, body: TechnicianIn, dealer_id: uuid.UUID) -> None:
    for attrname in (
        "name", "employee_id", "dms_tech_no", "team", "skill_level", "active",
        "shift_start", "shift_end", "lunch_start", "lunch_end",
        "max_daily_hours", "overtime_threshold", "efficiency_target", "productivity_target",
    ):
        setattr(t, attrname, getattr(body, attrname))
    t.work_days = list(body.work_days or [])

    t.certs = [
        TechnicianCert(
            dealer_id=dealer_id,
            cert_type=c.cert_type,
            level=c.level,
            expires_on=c.expires_on,
        )
        for c in body.certs
    ]
    t.restrictions = [
        TechnicianRestriction(dealer_id=dealer_id, blocked_work_type=w)
        for w in body.restrictions
    ]
    t.specialties = [
        TechnicianSpecialty(
            dealer_id=dealer_id,
            work_type=s.work_type,
            vehicle_specialty=s.vehicle_specialty,
        )
        for s in body.specialties
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_technician(body: TechnicianIn, session: SessionDep, current: CurrentUserDep):
    current.require_role("SERVICE_MANAGER")

    if body.dms_tech_no:
        clash = (
            await session.execute(
                select(Technician).where(
                    Technician.dealer_id == current.dealer_id,
                    Technician.dms_tech_no == body.dms_tech_no,
                )
            )
        ).scalar_one_or_none()
        if clash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"DMS tech # {body.dms_tech_no} already belongs to {clash.name}",
            )

    t = Technician(dealer_id=current.dealer_id, name=body.name)
    await _apply(session, t, body, current.dealer_id)
    session.add(t)
    await session.flush()

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.TECH_CREATED,
        entity="technician",
        entity_id=t.id,
        payload={"name": t.name, "missing_fields": t.missing_fields()},
    )
    await session.commit()
    await session.refresh(t)
    return _tech_dict(t)


@router.put("/{tech_id}")
async def update_technician(
    tech_id: uuid.UUID, body: TechnicianIn, session: SessionDep, current: CurrentUserDep
):
    current.require_role("SERVICE_MANAGER")
    t = await session.get(Technician, tech_id)
    if t is None or t.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Technician not found")

    await _apply(session, t, body, current.dealer_id)
    await session.flush()

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.TECH_UPDATED,
        entity="technician",
        entity_id=t.id,
        payload={"name": t.name, "missing_fields": t.missing_fields()},
    )
    await session.commit()
    await session.refresh(t)
    return _tech_dict(t)


@router.delete("/{tech_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_technician(tech_id: uuid.UUID, session: SessionDep, current: CurrentUserDep):
    current.require_role("ADMIN")
    t = await session.get(Technician, tech_id)
    if t is None or t.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Technician not found")

    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action=audit.TECH_DELETED,
        entity="technician",
        entity_id=t.id,
        payload={"name": t.name},
    )
    await session.delete(t)
    await session.commit()
