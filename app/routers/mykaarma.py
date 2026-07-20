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
)

router = APIRouter(prefix="/mykaarma", tags=["mykaarma"])


@router.get("/status")
async def status(session: SessionDep, current: CurrentUserDep):
    """Connection health: are creds present, does auth work, is RO scope granted?

    Safe to poll from the UI to show a "Connected to myKaarma / RO scope pending"
    badge on the Import screen.
    """
    return await connection_status(session, current.dealer_id)


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
            "myKaarma Order v2 UUIDs to ingest. Order v2 is read-by-UUID and "
            "myKaarma exposes no list endpoint, so supply the UUIDs (or wire "
            "webhooks) until an enumeration endpoint is available."
        ),
    )


@router.post("/sync/repair-orders")
async def sync_ros_route(
    session: SessionDep,
    current: CurrentUserDep,
    body: SyncROsRequest | None = None,
):
    """Pull repair orders from myKaarma (Order v2) and ingest them into the board."""
    current.require_role("SERVICE_MANAGER")
    result = await sync_repair_orders(
        session, current.dealer_id, (body.order_uuids if body else None)
    )
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
