"""Dispatcher timeline board (the Gantt dashboard)."""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter

from ..deps import CurrentUserDep, SessionDep
from ..services.timeline_service import build_timeline

router = APIRouter(prefix="/timeline", tags=["timeline"])


@router.get("")
async def get_timeline(
    session: SessionDep,
    current: CurrentUserDep,
    day: Optional[str] = None,
    include_off: bool = False,
):
    """Per-technician day timeline, built from real assignment rows.

    `day` is an ISO date (defaults to the shop's current day). `include_off`
    adds technicians who are not scheduled today.
    """
    on = date.fromisoformat(day) if day else None
    return await build_timeline(session, current.dealer_id, on=on, include_off=include_off)
