"""Repair-order CRUD.

V1 has no live DMS integration by design, so ROs arrive here — from the seed
script, or typed in.  This is the surface a live CDK/Reynolds/Tekion feed would
eventually write into.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..deps import CurrentUserDep, SessionDep
from ..models import RepairOrder, ROLine

router = APIRouter(prefix="/repair-orders", tags=["repair-orders"])

STATUSES = [
    "OPEN", "PENDING_AUTHORIZATION", "WAITING_ON_PARTS",
    "READY_TO_DISPATCH", "IN_PROGRESS", "COMPLETED",
]
FLAGS = ["MGR_FLAG", "HEAT_CASE", "COMEBACK", "WAITING"]


class ROLineIn(BaseModel):
    op_code: Optional[str] = None
    description: str
    flagged_hours: float = 0


class ROIn(BaseModel):
    ro_number: str
    vin: Optional[str] = None
    vehicle_year: Optional[int] = None
    vehicle_make: Optional[str] = None
    vehicle_model: Optional[str] = None
    mileage: Optional[int] = None
    concern_category: Optional[str] = None
    work_type: Optional[str] = None
    tier: Optional[str] = None
    required_certs: list[str] = Field(default_factory=list)
    required_team: Optional[str] = None
    est_hours: float = 0
    written_at: Optional[datetime] = None
    promise_at: Optional[datetime] = None
    status: str = "OPEN"
    flags: list[str] = Field(default_factory=list)
    priority: str = "MEDIUM"
    advisor_id: Optional[str] = None
    lines: list[ROLineIn] = Field(default_factory=list)


def _dict(ro: RepairOrder) -> dict:
    return {
        "id": str(ro.id),
        "ro_number": ro.ro_number,
        "vin": ro.vin,
        "vehicle_year": ro.vehicle_year,
        "vehicle_make": ro.vehicle_make,
        "vehicle_model": ro.vehicle_model,
        "mileage": ro.mileage,
        "concern_category": ro.concern_category,
        "work_type": ro.work_type,
        "tier": ro.tier,
        "required_certs": list(ro.required_certs or []),
        "required_team": ro.required_team,
        "est_hours": float(ro.est_hours or 0),
        "written_at": ro.written_at.isoformat() if ro.written_at else None,
        "promise_at": ro.promise_at.isoformat() if ro.promise_at else None,
        "status": ro.status,
        "flags": list(ro.flags or []),
        "priority": ro.priority,
        "advisor_id": ro.advisor_id,
        "lines": [
            {
                "op_code": ln.op_code,
                "description": ln.description,
                "flagged_hours": float(ln.flagged_hours or 0),
            }
            for ln in ro.lines
        ],
    }


@router.get("")
async def list_ros(
    session: SessionDep, current: CurrentUserDep, status_filter: Optional[str] = None
):
    q = select(RepairOrder).where(RepairOrder.dealer_id == current.dealer_id)
    if status_filter:
        q = q.where(RepairOrder.status == status_filter)
    ros = list((await session.execute(q.order_by(RepairOrder.written_at))).scalars())
    return {"repair_orders": [_dict(r) for r in ros]}


@router.get("/{ro_id}")
async def get_ro(ro_id: uuid.UUID, session: SessionDep, current: CurrentUserDep):
    ro = await session.get(RepairOrder, ro_id)
    if ro is None or ro.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repair order not found")
    return _dict(ro)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ro(body: ROIn, session: SessionDep, current: CurrentUserDep):
    if body.status not in STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"status must be one of {STATUSES}")

    clash = (
        await session.execute(
            select(RepairOrder).where(
                RepairOrder.dealer_id == current.dealer_id,
                RepairOrder.ro_number == body.ro_number,
            )
        )
    ).scalar_one_or_none()
    if clash:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"RO {body.ro_number} already exists"
        )

    ro = RepairOrder(
        dealer_id=current.dealer_id,
        **body.model_dump(exclude={"lines"}),
    )
    ro.lines = [
        ROLine(
            dealer_id=current.dealer_id,
            op_code=ln.op_code,
            description=ln.description,
            flagged_hours=ln.flagged_hours,
            sort_order=i,
        )
        for i, ln in enumerate(body.lines)
    ]
    session.add(ro)
    await session.commit()
    await session.refresh(ro)
    return _dict(ro)


class StatusIn(BaseModel):
    status: str


@router.put("/{ro_id}/status")
async def set_status(
    ro_id: uuid.UUID, body: StatusIn, session: SessionDep, current: CurrentUserDep
):
    if body.status not in STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"status must be one of {STATUSES}")
    ro = await session.get(RepairOrder, ro_id)
    if ro is None or ro.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repair order not found")
    ro.status = body.status
    await session.commit()
    return _dict(ro)
