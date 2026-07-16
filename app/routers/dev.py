"""Dev-only helpers.

Every route here is gated on AUTH_MODE=dev and 404s otherwise.  In production
the dealer comes from the caller's JWT (a user belongs to exactly one store),
so a "list all dealers" endpoint must never exist there.  It is here purely so
the demo UI can offer a dealer switcher and show the multi-tenant boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ..config import get_settings
from ..deps import SessionDep
from ..models import Dealer

router = APIRouter(prefix="/dev", tags=["dev"])
settings = get_settings()


@router.get("/dealers")
async def list_dealers(session: SessionDep):
    if settings.auth_mode != "dev":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not available")
    dealers = list((await session.execute(select(Dealer).order_by(Dealer.name))).scalars())
    return {
        "dealers": [
            {"id": str(d.id), "name": d.name, "timezone": d.timezone} for d in dealers
        ]
    }
