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

from ..config import get_settings
from ..deps import CurrentUserDep, SessionDep
from ..models import AuditLog, RepairOrder, WarrantyROAudit
from ..services.warranty_service import (
    WarrantyAuditError,
    audit_ro,
    get_rubric,
    process_ghl_webhook,
    set_rubric,
)
from ..db import utcnow

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


@router.post("/audit/batch")
async def audit_batch(session: SessionDep, current: CurrentUserDep, limit: Optional[int] = None):
    """Audit the store's open ROs (capped). Warranty vs customer-pay is classified
    per-RO by the auditor; the batch runs sequentially so a flaky network can't
    lose work already committed. Truncation is reported, never silent."""
    cap = min(limit or settings.warranty_batch_cap, settings.warranty_batch_cap)
    ros = list(
        (
            await session.execute(
                select(RepairOrder)
                .where(RepairOrder.dealer_id == current.dealer_id)
                .where(RepairOrder.status.notin_(["COMPLETED"]))
                .order_by(RepairOrder.written_at.desc())
            )
        ).scalars()
    )
    total = len(ros)
    batch = ros[:cap]
    audited, failed = 0, 0
    for ro in batch:
        try:
            await audit_ro(session, current.dealer_id, ro)
            audited += 1
        except WarrantyAuditError as exc:
            failed += 1
            log.warning("Batch audit failed for RO %s: %s", ro.ro_number, exc)
            if audited == 0 and failed == 1:
                # First call failed hard (e.g. no API key) — stop and tell the user.
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    truncated = max(0, total - len(batch))
    msg = f"Audited {audited} RO(s)"
    if failed:
        msg += f", {failed} failed"
    if truncated:
        msg += f". Capped at {cap} — {truncated} more not audited this run."
    return {"audited": audited, "failed": failed, "total_open": total, "capped_at": cap, "message": msg}


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
    """Public endpoint GHL's 'Trigger on Upload' workflow calls. We are the
    external processor: validate the shared secret, then audit + write back to
    GHL in the background so GHL's workflow never waits on Claude (pattern 3b)."""
    secret = settings.ghl_webhook_secret
    provided = token or request.headers.get("X-Webhook-Secret")
    if secret and provided != secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook secret")
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be JSON") from exc
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
