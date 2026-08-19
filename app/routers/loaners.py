"""Service Loaner module endpoints.

V1 surfaces live loaner DEMAND from myKaarma + an over-booking check. The TSD
checkout feed (which loaner is physically out, on which RO) plugs in later to
turn this into a full availability board.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import CurrentUserDep, SessionDep
from ..services.loaners_service import build_loaner_board

router = APIRouter(prefix="/loaners", tags=["loaners"])


@router.get("/board")
async def loaner_board(session: SessionDep, current: CurrentUserDep, days: int = 7):
    """Loaner Availability view: today's + upcoming loaner-needed appointments per
    store (live from myKaarma), plus a guaranteed-over-booking flag vs fleet size."""
    return await build_loaner_board(session, current.dealer_id, days)
