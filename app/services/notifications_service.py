"""Instant technician notification via the GHL API.

When a dispatcher assigns an RO, the tech should know immediately. myKaarma
can't write the assignment back into the DMS (confirmed with the vendor), so we
notify our own way, through GoHighLevel:

  1. upsert the technician as a GHL contact (cached on the tech after the first
     time, so all their job texts thread into one conversation), then
  2. send the assignment as an SMS through GHL's Conversations API (email as a
     fallback when there's no phone) — GHL's carrier connect does the sending.

The message shows in GHL under that tech's contact/conversation, and we TRACK
the outcome on the assignment so a failed text surfaces on the dispatch board
instead of a job sitting unacknowledged. Best-effort throughout: a notification
problem must never fail a dispatch.

Scope note: the GHL API key (Private Integration Token) must include
Contacts (write) and Conversations / Conversations Messages (write) scopes.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import utcnow
from ..models import Assignment, RepairOrder, Technician

settings = get_settings()
log = logging.getLogger("3d-dispatch.notify")


def _message(ro: RepairOrder, vehicle: str) -> str:
    veh = f" — {vehicle}" if vehicle else ""
    return (
        f"New job: RO #{ro.ro_number}{veh} has been assigned to you. "
        f"Open 3D Dispatch → My Work to start."
    )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.ghl_api_key}",
        "Version": "2021-07-28",
        "content-type": "application/json",
    }


async def _upsert_contact(client: httpx.AsyncClient, tech: Technician) -> str | None:
    """Find-or-create the tech as a GHL contact; return the contact id."""
    if tech.ghl_contact_id:
        return tech.ghl_contact_id
    body: dict = {"locationId": settings.ghl_location_id, "name": tech.name, "tags": ["technician"]}
    if tech.phone:
        body["phone"] = tech.phone
    if tech.email:
        body["email"] = tech.email
    r = await client.post(f"{settings.ghl_base}/contacts/upsert", json=body, headers=_headers())
    if r.status_code >= 300:
        log.warning("GHL contact upsert failed (%s): %s", r.status_code, r.text[:200])
        return None
    data = r.json()
    contact = data.get("contact") or data
    cid = contact.get("id") or contact.get("contactId")
    if cid:
        tech.ghl_contact_id = cid  # cache for next time (caller commits)
    return cid


async def _send_message(
    client: httpx.AsyncClient, contact_id: str, channel: str, message: str, ro_number: str
) -> tuple[bool, str | None, str | None]:
    """Send via the Conversations API. Returns (ok, message_id, error)."""
    body: dict = {"type": channel, "contactId": contact_id, "message": message}
    if channel == "Email":
        body["subject"] = f"New job assigned — RO #{ro_number}"
        body["html"] = f"<p>{message}</p>"
    r = await client.post(
        f"{settings.ghl_base}/conversations/messages", json=body, headers=_headers()
    )
    if r.status_code >= 300:
        return False, None, f"GHL responded {r.status_code}: {r.text[:150]}"
    data = r.json()
    return True, (data.get("messageId") or data.get("id")), None


async def notify_tech_assignment(session: AsyncSession, assignment: Assignment) -> None:
    """Best-effort: notify the assigned tech through GHL and record the outcome
    on the assignment. Never raises."""
    tech = await session.get(Technician, assignment.technician_id)
    ro = await session.get(RepairOrder, assignment.ro_id)
    if tech is None or ro is None:
        return

    # SMS preferred; email is the fallback (GHL "Email" message type).
    channel = "SMS" if tech.phone else ("Email" if tech.email else None)
    if channel is None:
        assignment.notify_status = "failed"
        assignment.notify_error = "No phone or email on file for this technician"
        await session.commit()
        return
    assignment.notify_channel = "SMS" if channel == "SMS" else "EMAIL"

    if not settings.ghl_configured:
        assignment.notify_status = "queued"
        assignment.notify_error = "GHL not configured yet (API key / location)"
        await session.commit()
        return

    vehicle = " ".join(
        str(x) for x in (ro.vehicle_year, ro.vehicle_make, ro.vehicle_model) if x
    )
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            contact_id = await _upsert_contact(client, tech)
            if not contact_id:
                raise RuntimeError("could not create the tech's GHL contact")
            ok, msg_id, err = await _send_message(
                client, contact_id, channel, _message(ro, vehicle), ro.ro_number
            )
        if ok:
            assignment.notify_status = "sent"
            assignment.notified_at = utcnow()
            assignment.notify_ref = msg_id
            assignment.notify_error = None
        else:
            assignment.notify_status = "failed"
            assignment.notify_error = err
            log.warning("Tech notify failed for RO %s: %s", ro.ro_number, err)
    except Exception as exc:  # noqa: BLE001 — never let a notify problem fail dispatch
        assignment.notify_status = "failed"
        assignment.notify_error = str(exc)[:200]
        log.warning("Tech notify error for RO %s: %s", ro.ro_number, exc)
    await session.commit()
