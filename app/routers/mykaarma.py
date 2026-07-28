"""myKaarma connector endpoints (live DMS integration)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import audit
from ..deps import CurrentUserDep, SessionDep
from ..mykaarma.connector import (
    connection_status,
    sync_opcodes,
    sync_repair_orders,
    upcoming_appointments,
)

router = APIRouter(prefix="/mykaarma", tags=["mykaarma"])


@router.get("/status")
async def status(session: SessionDep, current: CurrentUserDep):
    """Connection health: are creds present, does auth work, is RO scope granted?

    Safe to poll from the UI to show a "Connected to myKaarma / RO scope pending"
    badge on the Import screen.
    """
    return await connection_status(session, current.dealer_id)


@router.get("/appointments/upcoming")
async def upcoming(session: SessionDep, current: CurrentUserDep, days: int = 14):
    """Upcoming appointments booked in myKaarma (e.g. by the voice agent).

    Shown on the Available ROs → Upcoming ROs tab so the owner can watch a
    booking flow straight from the app.
    """
    return await upcoming_appointments(session, current.dealer_id, days)


@router.post("/sync/opcodes")
async def sync_opcodes_route(session: SessionDep, current: CurrentUserDep):
    """Pull the myKaarma service catalogue into the op-code map."""
    current.require_role("SERVICE_MANAGER")
    result = await sync_opcodes(session, current.dealer_id)
    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action="MYKAARMA_SYNC_OPCODES",
        entity="op_code_map",
        payload={"ok": result.ok, "message": result.message, **result.detail},
    )
    await session.commit()
    return {"ok": result.ok, "message": result.message, "detail": result.detail}


class SyncROsRequest(BaseModel):
    order_uuids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional: specific myKaarma Order v2 UUIDs to ingest (e.g. from a "
            "webhook). Leave empty to AUTO-ENUMERATE all open ROs via the "
            "specificSearch endpoint and ingest them."
        ),
    )


@router.post("/sync/repair-orders")
async def sync_ros_route(
    session: SessionDep,
    current: CurrentUserDep,
    body: SyncROsRequest | None = None,
):
    """Pull open repair orders from myKaarma and ingest them into the board.

    With no order_uuids it enumerates every open RO (specificSearch) and ingests
    them; pass explicit UUIDs to ingest just those.
    """
    current.require_role("SERVICE_MANAGER")
    # empty list -> auto-enumerate (None); only a non-empty list targets specific orders
    uuids = body.order_uuids if (body and body.order_uuids) else None
    result = await sync_repair_orders(session, current.dealer_id, uuids)
    await audit.record(
        session,
        dealer_id=current.dealer_id,
        actor=current.user_id,
        action="MYKAARMA_SYNC_ROS",
        entity="repair_order",
        payload={"ok": result.ok, "message": result.message, **result.detail},
    )
    await session.commit()
    return {"ok": result.ok, "message": result.message, "detail": result.detail}
