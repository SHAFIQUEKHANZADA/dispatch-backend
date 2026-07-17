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
from ..services.dispatch_service import load_shop

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


def _tech_dict(t: Technician) -> dict:
    return {
        "id": str(t.id),
        "name": t.name,
        "employee_id": t.employee_id,
        "dms_tech_no": t.dms_tech_no,
        "team": t.team,
        "skill_level": t.skill_level,
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
