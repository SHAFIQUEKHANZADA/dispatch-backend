"""Warranty RO Audit — read an RO's warranty documentation and score it against
the 12-check rubric BEFORE it's submitted to Honda/Acura.

Division of labour (same discipline as the rest of 3D Dispatch):
  - The JUDGEMENT of the RO (does the paperwork show a cause? a valid op code?)
    is done by Claude, which reads the full RO detail.
  - The RESULT is deterministic bookkeeping: we validate Claude's output against
    the rubric, force any missing/garbled check to `needs_review`, compute the
    overall status as the worst case, and store it.

GUARDIAN RULE: never fabricate a pass. If the live RO data doesn't SHOW what a
check needs (punch records, part numbers, DTC printouts, DPSM auth…), the check
is `needs_review` with a reason — it is never silently "passed".
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import SessionLocal
from ..services.ghl_common import GHLCreds, get_ghl_creds, ghl_creds_by_location
from ..models import (
    DEFAULT_WARRANTY_RUBRIC,
    WARRANTY_RESULTS,
    Assignment,
    Dealer,
    RepairOrder,
    Technician,
    WarrantyROAudit,
    WarrantyRubric,
)

settings = get_settings()
log = logging.getLogger("3d-dispatch.warranty")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# Ranking for the overall status: the worst finding wins.
_SEVERITY = {"fail": 3, "needs_review": 2, "pass": 1, "na": 0}


class WarrantyAuditError(Exception):
    """Raised when an audit cannot be run (e.g. no API key) — surfaced as a 4xx/5xx.
    We raise instead of returning a fake result, per the Guardian rule."""


# --------------------------------------------------------------------------- #
# rubric                                                                       #
# --------------------------------------------------------------------------- #


async def get_rubric(session: AsyncSession, dealer_id: uuid.UUID) -> list[dict]:
    """The dealer's editable rubric, seeded from the default on first read."""
    row = await session.get(WarrantyRubric, dealer_id)
    if row is None or not row.checks:
        return [dict(c) for c in DEFAULT_WARRANTY_RUBRIC]
    return [dict(c) for c in row.checks]


async def set_rubric(session: AsyncSession, dealer_id: uuid.UUID, checks: list[dict]) -> list[dict]:
    row = await session.get(WarrantyRubric, dealer_id)
    if row is None:
        row = WarrantyRubric(dealer_id=dealer_id, checks=checks)
        session.add(row)
    else:
        row.checks = checks
    await session.commit()
    return checks


# --------------------------------------------------------------------------- #
# building the RO context Claude reads                                         #
# --------------------------------------------------------------------------- #


async def _ro_context(session: AsyncSession, ro: RepairOrder) -> dict:
    """Everything we truly know about this RO, from the live DB. We hand Claude
    exactly this — no more — so it can only mark a check `pass` on real data."""
    # The tech (if any) assigned in 3D Dispatch — used for Technician ID Match.
    assignment = (
        await session.execute(
            select(Assignment)
            .where(Assignment.ro_id == ro.id)
            .order_by(Assignment.assigned_at.desc())
        )
    ).scalars().first()
    tech_name: Optional[str] = None
    tech_no: Optional[str] = None
    if assignment is not None:
        tech = await session.get(Technician, assignment.technician_id)
        if tech is not None:
            tech_name = tech.name
            tech_no = tech.dms_tech_no

    return {
        "ro_number": ro.ro_number,
        "vin": ro.vin,
        "vehicle": " ".join(
            str(x) for x in (ro.vehicle_year, ro.vehicle_make, ro.vehicle_model) if x
        ) or None,
        "mileage": ro.mileage,
        "status": ro.status,
        "concern_category": ro.concern_category,
        "work_type": ro.work_type,
        "advisor_id": ro.advisor_id,
        "est_hours": float(ro.est_hours or 0),
        "assigned_technician": tech_name,
        "assigned_technician_dms_no": tech_no,
        "lines": [
            {
                "op_code": ln.op_code,
                "description": ln.description,
                "flagged_hours": float(ln.flagged_hours or 0),
            }
            for ln in ro.lines
        ],
    }


def _system_prompt(rubric: list[dict]) -> str:
    lines = [
        "You are a warranty documentation auditor for a Honda/Acura dealership "
        "service department. You audit a repair order (RO) BEFORE it is submitted "
        "to the manufacturer for warranty reimbursement.",
        "",
        "You are given ONLY the structured RO data that exists in the dealership's "
        "system. You must judge each of the checks below against THAT data alone.",
        "",
        "THE GUARDIAN RULE — this is the most important instruction:",
        "  * NEVER mark a check 'pass' unless the provided data actually SHOWS the "
        "required information. A plausible assumption is not evidence.",
        "  * If the data does not contain what a check needs (e.g. no punch "
        "records, no part numbers, no DTC printout, no DPSM authorization), you "
        "MUST return 'needs_review' — never 'pass'. A human will then look.",
        "  * Use 'fail' only when the data present is clearly wrong, contradictory, "
        "or missing something that IS expected to be on the RO itself (e.g. no VIN).",
        "  * Use 'na' only when the check genuinely does not apply to this RO "
        "(e.g. Battery Tester on an RO with no battery work).",
        "  * Every finding must include a short, concrete reason citing what you "
        "saw or what was missing.",
        "",
        "THE CHECKS (return one finding for EACH, using the exact check name):",
    ]
    for i, c in enumerate(rubric, 1):
        lines.append(f"  {i}. {c['name']} — {c.get('needs','')} (DOM: {c.get('dom_section','')})")
    lines += [
        "",
        "Also determine job_line_type: one of 'Warranty', 'Customer Pay', "
        "'Internal', 'Service Contract' — or 'Unknown' if the data doesn't say.",
        "",
        "Return STRICT JSON ONLY, no prose, no markdown fences, in exactly this shape:",
        '{"job_line_type":"<type>","findings":[{"check":"<exact name>",'
        '"result":"pass|needs_review|fail|na","dom_section":"<citation or empty>",'
        '"reason":"<short reason>"}]}',
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the Claude call (resilient, like the myKaarma client)                        #
# --------------------------------------------------------------------------- #


# Forcing a tool call makes Anthropic return guaranteed-valid structured JSON
# (as the tool's `input`), so a stray quote in a reason can never break parsing.
_AUDIT_TOOL = {
    "name": "submit_warranty_audit",
    "description": "Return the warranty documentation audit for the repair order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "job_line_type": {
                "type": "string",
                "enum": ["Warranty", "Customer Pay", "Internal", "Service Contract", "Unknown"],
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "check": {"type": "string"},
                        "result": {"type": "string", "enum": list(WARRANTY_RESULTS)},
                        "dom_section": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["check", "result", "reason"],
                },
            },
        },
        "required": ["job_line_type", "findings"],
    },
}


async def _call_claude(system: str, user_content: Any) -> dict:
    """Returns the auditor's structured output as a dict. user_content is a
    plain string or a list of content blocks (text / document / image), so the
    same call serves the structured-RO path and the uploaded-scan path. We force
    a tool call so the result is always valid JSON."""
    if not settings.anthropic_configured:
        raise WarrantyAuditError(
            "ANTHROPIC_API_KEY is not configured — add it to backend/.env to run audits."
        )
    body = {
        "model": settings.warranty_audit_model,
        "max_tokens": 3000,
        "system": system,
        "tools": [_AUDIT_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_warranty_audit"},
        "messages": [{"role": "user", "content": user_content}],
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    last: Exception | None = None
    for attempt, backoff in enumerate((1.5, 3.0, 4.5, 0), start=1):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(ANTHROPIC_URL, json=body, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                for b in data.get("content", []):
                    if b.get("type") == "tool_use":
                        return b.get("input") or {}
                # Fallback: model answered in text instead of the tool.
                parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
                return _parse_json("".join(parts))
            # 429 / 5xx are worth retrying; 4xx (bad key, bad request) are not.
            if resp.status_code in (429, 500, 502, 503, 529) and backoff:
                await asyncio.sleep(backoff)
                continue
            raise WarrantyAuditError(
                f"Anthropic API error {resp.status_code}: {resp.text[:300]}"
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
            last = exc
            if backoff:
                await asyncio.sleep(backoff)
                continue
    raise WarrantyAuditError(f"Could not reach Anthropic after retries: {last}")


def _parse_json(text: str) -> dict:
    """Claude is asked for bare JSON, but strip a ```json fence just in case."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    # If there's leading/trailing prose, grab the outermost JSON object.
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start : end + 1]
    return json.loads(t)


# --------------------------------------------------------------------------- #
# deterministic result assembly (the Guardian layer)                          #
# --------------------------------------------------------------------------- #


def _reconcile(rubric: list[dict], model_out: dict) -> tuple[list[dict], str, str]:
    """Force the model's output onto the rubric. EVERY rubric check must appear
    exactly once; anything missing or invalid becomes `needs_review`. This is
    what guarantees a check can never be silently dropped or fabricated."""
    by_name: dict[str, dict] = {}
    for f in model_out.get("findings", []) or []:
        name = str(f.get("check", "")).strip()
        if name:
            by_name[name.lower()] = f

    findings: list[dict] = []
    for c in rubric:
        f = by_name.get(c["name"].lower())
        result = str((f or {}).get("result", "")).strip().lower()
        if result not in WARRANTY_RESULTS:
            # missing or garbled -> needs a human, never a pass
            findings.append({
                "check": c["name"],
                "result": "needs_review",
                "dom_section": c.get("dom_section", ""),
                "reason": "The auditor did not return a valid result for this check — needs review.",
            })
            continue
        findings.append({
            "check": c["name"],
            "result": result,
            "dom_section": str((f or {}).get("dom_section") or c.get("dom_section", "")),
            "reason": str((f or {}).get("reason") or "").strip() or "No reason provided — needs review.",
        })

    # Overall status = worst case. If nothing is worse than na/pass but at least
    # one real 'pass' exists -> pass; an all-'na' RO is not a pass.
    worst = max((_SEVERITY[f["result"]] for f in findings), default=0)
    if worst == 3:
        status = "fail"
    elif worst == 2:
        status = "needs_review"
    elif any(f["result"] == "pass" for f in findings):
        status = "pass"
    else:
        status = "needs_review"

    job_type = str(model_out.get("job_line_type") or "Unknown").strip() or "Unknown"
    return findings, status, job_type


# --------------------------------------------------------------------------- #
# public API                                                                   #
# --------------------------------------------------------------------------- #


async def _upsert_audit(
    session: AsyncSession,
    dealer_id: uuid.UUID,
    ro_number: str,
    *,
    vin: Optional[str],
    technician_id: Optional[str],
    job_type: str,
    audit_status: str,
    findings: list[dict],
    source_ro_id: Optional[uuid.UUID],
) -> WarrantyROAudit:
    row = (
        await session.execute(
            select(WarrantyROAudit).where(
                WarrantyROAudit.dealer_id == dealer_id,
                WarrantyROAudit.ro_number == ro_number,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = WarrantyROAudit(dealer_id=dealer_id, ro_number=ro_number)
        session.add(row)
    row.vin = vin
    row.technician_id = technician_id
    row.job_line_type = job_type
    row.source_ro_id = source_ro_id
    row.audit_status = audit_status
    row.findings = findings
    # A fresh audit resets a prior human decision — the paperwork changed.
    row.reviewer_decision = "not_reviewed"
    row.reviewer_notes = None
    await session.commit()
    await session.refresh(row)
    return row


async def audit_ro(
    session: AsyncSession, dealer_id: uuid.UUID, ro: RepairOrder, *, push_ghl: bool = True
) -> WarrantyROAudit:
    """Run the 12 checks on one live RO (structured data) and upsert its audit row."""
    rubric = await get_rubric(session, dealer_id)
    ctx = await _ro_context(session, ro)
    user_content = [
        {
            "type": "text",
            "text": (
                "Audit this repair order. Here is the complete RO data available "
                "in our system:\n\n" + json.dumps(ctx, indent=2, default=str)
            ),
        }
    ]
    model_out = await _call_claude(_system_prompt(rubric), user_content)
    findings, audit_status, job_type = _reconcile(rubric, model_out)
    row = await _upsert_audit(
        session, dealer_id, ro.ro_number,
        vin=ro.vin,
        technician_id=ctx.get("assigned_technician_dms_no") or ctx.get("assigned_technician"),
        job_type=job_type, audit_status=audit_status, findings=findings, source_ro_id=ro.id,
    )

    # Best-effort mirror to GHL — into THIS store's own GHL account. Each store's
    # creds are looked up by dealer, so a Honda audit lands in Honda's GHL and
    # never in Acura's. A store with no GHL wired simply skips the push.
    if push_ghl:
        creds = await get_ghl_creds(session, dealer_id)
        if creds is not None:
            try:
                await sync_to_ghl(row, creds)
            except Exception as exc:  # noqa: BLE001
                log.warning("GHL sync failed for RO %s: %s", ro.ro_number, exc)
    return row


async def audit_file(session: AsyncSession, dealer_id: uuid.UUID, meta: dict) -> WarrantyROAudit:
    """Audit an uploaded RO scan/PDF (the secondary path) when the RO isn't in
    our live DB. Claude reads the document itself. `meta` carries known fields
    from GHL (ro_number, vin, technician_id, job_line_type, file_url)."""
    rubric = await get_rubric(session, dealer_id)
    file_url = meta.get("file_url")
    if not file_url:
        raise WarrantyAuditError("No RO found in our system and no file to read.")
    blob, media_type = await _download(file_url)
    b64 = base64.b64encode(blob).decode()
    if media_type == "application/pdf":
        doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    elif media_type in _IMAGE_TYPES:
        doc = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    else:
        raise WarrantyAuditError(f"Unsupported RO file type: {media_type}")

    hint = {k: meta.get(k) for k in ("ro_number", "vin", "technician_id", "job_line_type") if meta.get(k)}
    text = {
        "type": "text",
        "text": (
            "Audit this repair order document (a scan or PDF of the actual RO). "
            "Read the document and apply every check to what you can actually see."
            + (f"\n\nKnown fields from the system: {json.dumps(hint)}" if hint else "")
        ),
    }
    model_out = await _call_claude(_system_prompt(rubric), [doc, text])
    findings, audit_status, job_type = _reconcile(rubric, model_out)
    ro_number = str(meta.get("ro_number") or "").strip() or f"upload-{uuid.uuid4().hex[:8]}"
    return await _upsert_audit(
        session, dealer_id, ro_number,
        vin=meta.get("vin"), technician_id=meta.get("technician_id"),
        job_type=meta.get("job_line_type") or job_type, audit_status=audit_status,
        findings=findings, source_ro_id=None,
    )


async def _download(url: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise WarrantyAuditError(f"Could not download RO file ({resp.status_code}).")
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not ctype or ctype == "application/octet-stream":
        low = url.lower()
        ctype = (
            "application/pdf" if ".pdf" in low
            else "image/png" if ".png" in low
            else "image/jpeg" if (".jpg" in low or ".jpeg" in low)
            else ctype
        )
    return resp.content, ctype


# --------------------------------------------------------------------------- #
# GoHighLevel sync (optional)                                                  #
# --------------------------------------------------------------------------- #

# check name -> GHL single-option field key (option keys are the result values).
_GHL_CHECK_FIELDS = {c["name"]: c["key"] for c in DEFAULT_WARRANTY_RUBRIC}
# Allowed option keys for the dropdown fields. GHL rejects the whole record if a
# single-option field is sent a value outside its list (an empty string too), so
# we only send these fields when the value is a real option.
_RESULT_OPTIONS = {"pass", "needs_review", "fail", "na"}
_JOB_TYPE_OPTIONS = {"warranty", "customer_pay", "internal", "service_contract"}


def _ghl_props(row: WarrantyROAudit) -> dict[str, Any]:
    """Map our audit onto the GHL custom-object field keys (the section-6 shape).
    TEXT fields are always safe; single-option (dropdown) fields are only sent
    when we hold a value that's actually one of their options — GHL 400s the
    whole record on an out-of-list value (including an empty string)."""
    props: dict[str, Any] = {
        "audit_status": row.audit_status,          # always pending|pass|needs_review|fail
        "findings_json": json.dumps(row.findings),
        "ro_number": row.ro_number,
        "vin": row.vin or "",                      # TEXT — "" is fine
        "technician_id": row.technician_id or "",  # TEXT — "" is fine
    }
    # Job Line Type is a dropdown — only include it when classified.
    if row.job_line_type in _JOB_TYPE_OPTIONS:
        props["job_line_type"] = row.job_line_type
    for f in row.findings or []:
        key = _GHL_CHECK_FIELDS.get(f.get("check", ""))
        result = f.get("result")
        if key and result in _RESULT_OPTIONS:
            props[key] = result
    return props


async def sync_to_ghl(row: WarrantyROAudit, creds: GHLCreds) -> None:
    """CREATE a GHL record in the store's own account — used when an audit
    originates in 3D Dispatch (there is no GHL record yet)."""
    payload = {"locationId": creds.location_id, "properties": _ghl_props(row)}
    url = f"{settings.ghl_base}/objects/{creds.object_key}/records"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload, headers=creds.headers())
        if r.status_code >= 300:
            log.warning("GHL create failed (%s): %s", r.status_code, r.text[:200])


async def push_audit_to_ghl(
    record_id: str, row: WarrantyROAudit, creds: GHLCreds, callback_url: Optional[str] = None
) -> None:
    """UPDATE an existing GHL record by id, in the store's own account — the
    callback path (3b) so the write lands on the record GHL already created on
    upload, and GHL's native activity log attributes the change (section 8).

    NOTE: unlike record *create* (POST, locationId in the body), record *update*
    (PUT) wants locationId as a QUERY param and the body as just {properties}.
    Sending locationId in the PUT body 422s the whole write-back."""
    if callback_url:
        url, params = callback_url, {}
    else:
        url = f"{settings.ghl_base}/objects/{creds.object_key}/records/{record_id}"
        params = {"locationId": creds.location_id}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.put(
            url, params=params, json={"properties": _ghl_props(row)}, headers=creds.headers()
        )
        if r.status_code >= 300:
            log.warning("GHL update failed (%s): %s", r.status_code, r.text[:200])


# --------------------------------------------------------------------------- #
# GHL inbound webhook — 3D Dispatch is the "external processor" (section 10)   #
# --------------------------------------------------------------------------- #


async def _ro_in_store(
    session: AsyncSession, dealer_id: uuid.UUID, ro_number: Optional[str]
) -> Optional[RepairOrder]:
    """The live RO in THIS store matching the number, if any."""
    if not ro_number:
        return None
    return (
        await session.execute(
            select(RepairOrder).where(
                RepairOrder.dealer_id == dealer_id,
                RepairOrder.ro_number == ro_number,
            )
        )
    ).scalars().first()


async def process_ghl_webhook(payload: dict) -> None:
    """Run on upload: resolve which store this webhook is from (by its GHL
    location id), audit the RO (from our DB if we have it, else from the uploaded
    file), and write the result back into that store's OWN GHL record."""
    record_id = payload.get("record_id")
    callback_url = payload.get("callback_url")
    ro_number = payload.get("ro_number")
    location_id = payload.get("location_id") or payload.get("locationId")
    async with SessionLocal() as session:
        creds = await ghl_creds_by_location(session, location_id) if location_id else None
        if creds is None:
            log.warning(
                "GHL webhook: no store matches location %s (RO %s) — skipped.",
                location_id, ro_number,
            )
            return
        dealer_id = creds.dealer_id
        ro = await _ro_in_store(session, dealer_id, ro_number)
        try:
            if ro is not None:
                row = await audit_ro(session, dealer_id, ro, push_ghl=False)
            else:
                row = await audit_file(session, dealer_id, payload)
        except WarrantyAuditError as exc:
            log.warning("GHL webhook audit failed for RO %s: %s", ro_number, exc)
            return
        if record_id:
            try:
                await push_audit_to_ghl(record_id, row, creds, callback_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("GHL write-back failed for RO %s: %s", ro_number, exc)
