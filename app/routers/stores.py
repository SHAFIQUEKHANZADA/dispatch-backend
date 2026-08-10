"""Store registry — the multi-tenant switcher backend.

Lists the stores the current user may view. A group owner (ADMIN) sees every
store in the registry; a single-store user sees only their own. Adding a store
is an INSERT here (dealer row + its myKaarma creds), never a new deployment.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from ..deps import CurrentUserDep, SessionDep
from ..models import Dealer, MyKaarmaDealer, RepairOrder

router = APIRouter(prefix="/stores", tags=["stores"])


@router.get("")
async def list_stores(session: SessionDep, current: CurrentUserDep):
    """Stores the current user can switch between (ADMIN = all; else own only)."""
    q = select(Dealer).order_by(Dealer.created_at)
    if current.role != "ADMIN":
        q = q.where(Dealer.id == current.dealer_id)
    dealers = list((await session.execute(q)).scalars())

    # which stores have myKaarma creds configured (per-store row)
    configured = {
        r[0] for r in (await session.execute(select(MyKaarmaDealer.dealer_id))).all()
    }
    # RO counts per store (cheap health signal for the switcher)
    counts = {
        r[0]: r[1]
        for r in (
            await session.execute(
                select(RepairOrder.dealer_id, func.count()).group_by(RepairOrder.dealer_id)
            )
        ).all()
    }

    stores = [
        {
            "dealer_id": str(d.id),
            "store_id": d.dealer_key,          # the stable key (aka dealer_key)
            "name": d.name,
            "timezone": d.timezone,
            "mykaarma_configured": d.id in configured,
            "ro_count": counts.get(d.id, 0),
            "is_current": d.id == current.dealer_id,
        }
        for d in dealers
    ]
    return {
        "current_store_id": next(
            (s["store_id"] for s in stores if s["is_current"]), None
        ),
        "stores": stores,
    }
