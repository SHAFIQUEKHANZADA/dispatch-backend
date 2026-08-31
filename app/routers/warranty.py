"""Warranty RO Audit endpoints.

Audits an RO's warranty documentation before it's submitted to Honda/Acura.
Claude reads the RO; the result is deterministic and every check cites WHY.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..db import SessionLocal, utcnow
from ..deps import CurrentUserDep, SessionDep
from ..models import AuditLog, RepairOrder, WarrantyROAudit
from ..services.ghl_common import ghl_creds_by_location
from ..services.warranty_service import (
    WarrantyAuditError,
    audit_ro,
    get_rubric,
    process_ghl_webhook,
    run_batch_audit,
    set_rubric,
)

settings = get_settings()
log = logging.getLogger("3d-dispatch.warranty")
router = APIRouter(prefix="/warranty", tags=["warranty"])


def _dict(a: WarrantyROAudit) -> dict:
    return {
        "id": str(a.id),
        "ro_number": a.ro_number,
        "vin": a.vin,
        "technician_id": a.technician_id,
        "job_line_type": a.job_line_type,
        "source_ro_id": str(a.source_ro_id) if a.source_ro_id else None,
        "audit_status": a.audit_status,
        "findings": a.findings or [],
        "reviewer_decision": a.reviewer_decision,
        "reviewer_notes": a.reviewer_notes,
        "submitted": a.submitted,
        "date_submitted": a.date_submitted.isoformat() if a.date_submitted else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


@router.get("/audits")
async def list_audits(session: SessionDep, current: CurrentUserDep):
    rows = list(
        (
            await session.execute(
                select(WarrantyROAudit)
                .where(WarrantyROAudit.dealer_id == current.dealer_id)
                .order_by(WarrantyROAudit.updated_at.desc())
            )
        ).scalars()
    )
    order = {"fail": 0, "needs_review": 1, "pending": 2, "pass": 3}
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.audit_status] = counts.get(r.audit_status, 0) + 1
    return {
        "audits": [_dict(r) for r in rows],
        "counts": counts,
        "anthropic_configured": settings.anthropic_configured,
        "ghl_configured": settings.ghl_configured,
    }


@router.get("/audits/{audit_id}")
async def get_audit(audit_id: uuid.UUID, session: SessionDep, current: CurrentUserDep):
    a = await session.get(WarrantyROAudit, audit_id)
    if a is None or a.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    return _dict(a)


# NOTE: `/audit/batch` MUST be declared before `/audit/{ro_id}`. Starlette matches
# routes in order, so a dynamic `/audit/{ro_id}` declared first would swallow the
# literal "batch" and try to parse it as a UUID.
@router.post("/audit/batch")
async def audit_batch(
    session: SessionDep, current: CurrentUserDep, background: BackgroundTasks,
    limit: Optional[int] = None, force: bool = False,
):
    """Audit the store's open ROs (capped), in the BACKGROUND. Normally skips ROs
    already audited so a click doesn't re-spend Claude on the same answer and the
    browser never waits minutes (it would time out with 'Failed to fetch'). Pass
    force=true to RE-AUDIT already-audited ROs too — used after new RO data is
    pulled (fresh tech/punch/parts) so existing audits refresh with it. Returns
    immediately with how many were queued; the page refreshes as they land."""
    if not settings.anthropic_configured:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Anthropic key not configured — audits can't run.",
        )
    cap = min(limit or settings.warranty_batch_cap, settings.warranty_batch_cap)

    # ROs already audited for this store — skip them so a click doesn't re-spend,
    # UNLESS force=true (re-audit to pick up freshly-pulled data).
    done = set() if force else set(
        (
            await session.execute(
                select(WarrantyROAudit.ro_number).where(
                    WarrantyROAudit.dealer_id == current.dealer_id
                )
            )
        ).scalars()
    )
    open_ros = list(
        (
            await session.execute(
                select(RepairOrder)
                .options(selectinload(RepairOrder.lines))
                .where(RepairOrder.dealer_id == current.dealer_id)
                .where(RepairOrder.status.notin_(["COMPLETED"]))
                .order_by(RepairOrder.written_at.desc())
            )
        ).scalars()
    )
    # Only warranty-relevant ROs (Michael's ask): keep an RO if it has a warranty
    # line, or if its lines have no labor type yet (not synced) — never exclude on
    # missing data. ROs that are known to be all customer-pay/internal are skipped.
    def _warranty_relevant(ro: RepairOrder) -> bool:
        known = [ln.labor_type for ln in ro.lines if ln.labor_type]
        return (not known) or ("WARRANTY" in known)

    pending = [
        ro for ro in open_ros if ro.ro_number not in done and _warranty_relevant(ro)
    ]
    batch = pending[:cap]

    if not batch:
        return {
            "queued": 0,
            "message": "No warranty ROs to audit right now — all caught up.",
        }

    background.add_task(run_batch_audit, current.dealer_id, [ro.id for ro in batch])
    remaining = max(0, len(pending) - len(batch))
    verb = "Re-auditing" if force else "Auditing"
    what = "RO(s)" if force else "new RO(s)"
    msg = f"{verb} {len(batch)} {what} in the background — this page will update as results land."
    if remaining:
        msg += f" ({remaining} more will need another run.)"
    return {"queued": len(batch), "remaining": remaining, "message": msg}


@router.post("/audit/{ro_id}")
async def audit_one(ro_id: uuid.UUID, session: SessionDep, current: CurrentUserDep):
    ro = await session.get(RepairOrder, ro_id)
    if ro is None or ro.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repair order not found")
    try:
        row = await audit_ro(session, current.dealer_id, ro)
    except WarrantyAuditError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return _dict(row)


class ReviewIn(BaseModel):
    decision: str = Field(pattern="^(confirmed|overridden)$")
    notes: Optional[str] = None
    submitted: Optional[bool] = None


@router.post("/{audit_id}/review")
async def review_audit(
    audit_id: uuid.UUID, body: ReviewIn, session: SessionDep, current: CurrentUserDep
):
    a = await session.get(WarrantyROAudit, audit_id)
    if a is None or a.dealer_id != current.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    if body.decision == "overridden" and not (body.notes and body.notes.strip()):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "A note is required to override an audit result."
        )
    a.reviewer_decision = body.decision
    a.reviewer_notes = body.notes.strip() if body.notes else None
    if body.submitted:
        a.submitted = True
        a.date_submitted = utcnow()

    session.add(
        AuditLog(
            dealer_id=current.dealer_id,
            actor=current.user_id,
            action=f"warranty_{body.decision}",
            entity="warranty_ro_audit",
            entity_id=a.id,
            payload={
                "ro_number": a.ro_number,
                "decision": body.decision,
                "notes": a.reviewer_notes,
                "submitted": bool(body.submitted),
                "audit_status": a.audit_status,
            },
        )
    )
    await session.commit()
    await session.refresh(a)
    return _dict(a)


@router.post("/ghl/webhook")
async def ghl_webhook(request: Request, background: BackgroundTasks, token: Optional[str] = None):
    """Public endpoint each store's GHL 'Trigger on Upload' workflow calls. We are
    the external processor. Multi-store: the payload carries the store's GHL
    `location_id`, so we resolve which store it is, validate THAT store's shared
    secret, then audit + write back in the background (pattern 3b) so GHL's
    workflow never waits on Claude."""
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be JSON") from exc

    location_id = payload.get("location_id") or payload.get("locationId")
    if not location_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Missing location_id — include the GHL Location Id in the webhook body.",
        )
    provided = token or request.headers.get("X-Webhook-Secret")
    async with SessionLocal() as session:
        creds = await ghl_creds_by_location(session, location_id)
    if creds is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown store (location_id)")
    if creds.webhook_secret and provided != creds.webhook_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook secret")

    background.add_task(process_ghl_webhook, payload)
    return {"status": "accepted", "ro_number": payload.get("ro_number")}


class RubricIn(BaseModel):
    checks: list[dict]


@router.get("/rubric")
async def read_rubric(session: SessionDep, current: CurrentUserDep):
    return {"checks": await get_rubric(session, current.dealer_id)}


@router.put("/rubric")
async def write_rubric(body: RubricIn, session: SessionDep, current: CurrentUserDep):
    current.require_role("SERVICE_MANAGER")
    if not body.checks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Rubric cannot be empty.")
    for c in body.checks:
        if not c.get("name"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Every check needs a name.")
        c.setdefault("key", str(c["name"]).lower().replace(" ", "_"))
        c.setdefault("needs", "")
        c.setdefault("dom_section", "")
    return {"checks": await set_rubric(session, current.dealer_id, body.checks)}
