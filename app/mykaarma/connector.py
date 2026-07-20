"""myKaarma connector — turns myKaarma API responses into 3D Dispatch rows.

Credential resolution: prefer the per-dealer row in mykaarma_dealers; fall back
to the env vars (the sandbox / single-store case). This is what lets more stores
be added without a redeploy.

The RO sync is deliberately gated: it *attempts* the myKaarma repair-order
endpoints, and when they come back non-JSON (scope not granted — the state
today) it records that honestly and signals the caller to keep using the CSV
importer for RO data. It never fabricates ROs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import (
    Assignment,
    MyKaarmaDealer,
    OpCodeMap,
    RepairOrder,
    ROLine,
    Technician,
)
from .client import (
    MyKaarmaClient,
    MyKaarmaCreds,
    MyKaarmaError,
    ScopeNotGrantedError,
)
from .mapping import map_order

settings = get_settings()


@dataclass
class SyncResult:
    ok: bool
    message: str
    detail: dict


async def resolve_creds(session: AsyncSession, dealer_id: uuid.UUID) -> Optional[MyKaarmaCreds]:
    """Per-dealer row first, then env fallback. None if neither is configured."""
    row = await session.get(MyKaarmaDealer, dealer_id)
    if row and row.enabled:
        return MyKaarmaCreds(
            username=row.username,
            password=row.password,
            dealer_uuid=row.dealer_uuid,
            department_uuid=row.department_uuid,
        )
    if settings.mykaarma_env_configured:
        return MyKaarmaCreds(
            username=settings.mykaarma_username,
            password=settings.mykaarma_password,
            dealer_uuid=settings.mykaarma_dealer_uuid,
            department_uuid=settings.mykaarma_department_uuid,
        )
    return None


async def _record(
    session: AsyncSession,
    dealer_id: uuid.UUID,
    *,
    status: str,
    detail: dict,
    ro_scope: Optional[bool] = None,
) -> None:
    """Persist what the last sync saw, so ops/UI can see connection health."""
    row = await session.get(MyKaarmaDealer, dealer_id)
    if row is None:
        return  # env-only sandbox; nothing to write back to
    row.last_synced_at = datetime.now(timezone.utc)
    row.last_sync_status = status
    row.last_sync_detail = detail
    if ro_scope is not None:
        row.ro_scope_granted = ro_scope


async def connection_status(session: AsyncSession, dealer_id: uuid.UUID) -> dict:
    """Live health check: are creds present, does auth work, is RO scope granted?"""
    creds = await resolve_creds(session, dealer_id)
    if creds is None:
        return {
            "configured": False,
            "reachable": False,
            "ro_scope_granted": False,
            "message": "No myKaarma credentials for this dealer (no row and no env fallback).",
        }

    client = MyKaarmaClient(creds)
    out: dict = {"configured": True, "dealer_uuid": creds.dealer_uuid[:12] + "…"}

    # 1. can we authenticate + get JSON at all?
    try:
        ping = client.ping()
        out["reachable"] = True
        out["opcode_total"] = ping.get("opcode_total")
    except MyKaarmaError as e:
        out["reachable"] = False
        out["message"] = str(e)
        return out

    # 2. is the repair-order scope granted? Probe the DOCUMENTED Order v2
    #    endpoint (read-by-UUID with a nil UUID). Today myKaarma returns a clean
    #    403 "ApiScope does not exist" — the endpoint is real, the scope isn't.
    try:
        granted = client.probe_ro_scope()
        out["ro_scope_granted"] = granted
        out["message"] = (
            "Connected. Repair-order scope (Order v2 API) IS granted."
            if granted
            else "Connected, but the Order v2 (repair-order) scope is NOT provisioned for "
            "this account yet — RO data falls back to CSV import. myKaarma must grant it."
        )
    except MyKaarmaError as e:
        out["ro_scope_granted"] = False
        out["message"] = f"Connected; RO scope probe failed: {e}"

    return out


async def sync_opcodes(session: AsyncSession, dealer_id: uuid.UUID) -> SyncResult:
    """Pull the myKaarma service catalogue into op_code_map.

    Existing rows keep their concern_category / tier (a human mapped those in the
    onboarding session); we only refresh the myKaarma-provided fields and add any
    new opcodes as 'Uncategorised' for the SM to classify.
    """
    creds = await resolve_creds(session, dealer_id)
    if creds is None:
        return SyncResult(False, "No myKaarma credentials for this dealer.", {})

    client = MyKaarmaClient(creds)
    try:
        data = client.search_opcodes(result_size=200)
    except MyKaarmaError as e:
        await _record(session, dealer_id, status="OPCODE_SYNC_FAILED", detail={"error": str(e)})
        await session.commit()
        return SyncResult(False, f"Opcode sync failed: {e}", {})

    ops = data.get("operationDTOList", []) or []
    existing = {
        r.op_code: r
        for r in (
            await session.execute(
                select(OpCodeMap).where(OpCodeMap.dealer_id == dealer_id)
            )
        ).scalars()
    }

    added, updated, dummy = 0, 0, 0
    for op in ops:
        code = (op.get("laborOpCode") or "").strip()
        if not code:
            continue
        if code.upper().startswith("DUMMY"):
            dummy += 1
        desc = op.get("description") or op.get("opCodeName")
        row = existing.get(code)
        if row is None:
            session.add(
                OpCodeMap(
                    dealer_id=dealer_id,
                    op_code=code,
                    concern_category="Uncategorised",  # SM classifies in onboarding
                    tier="B",
                    mykaarma_uuid=op.get("uuid"),
                    duration_minutes=op.get("opCodeDurationInMinutes"),
                    source="MYKAARMA",
                )
            )
            added += 1
        else:
            row.mykaarma_uuid = op.get("uuid")
            row.duration_minutes = op.get("opCodeDurationInMinutes")
            if row.source == "MANUAL":
                row.source = "MYKAARMA"
            updated += 1

    detail = {
        "opcodes_returned": len(ops),
        "added": added,
        "updated": updated,
        "dummy_placeholders": dummy,
        "total_count": data.get("totalCount"),
    }
    await _record(session, dealer_id, status="OPCODE_SYNC_OK", detail=detail)
    await session.commit()

    note = ""
    if dummy and len(ops) == dummy:
        note = " (sandbox only has DUMMYOPCODE — request real opcodes from myKaarma)"
    return SyncResult(True, f"Synced {len(ops)} opcode(s): {added} new, {updated} updated{note}", detail)


async def sync_repair_orders(
    session: AsyncSession,
    dealer_id: uuid.UUID,
    order_uuids: Optional[list[str]] = None,
) -> SyncResult:
    """Pull ROs from myKaarma (Order v2) and ingest them.

    Order v2 is read-by-UUID. Pass `order_uuids` to ingest specific orders. With
    no UUIDs we report that ingestion is ready but enumeration is unavailable —
    we never invent orders.
    """
    creds = await resolve_creds(session, dealer_id)
    if creds is None:
        return SyncResult(False, "No myKaarma credentials for this dealer.", {})

    client = MyKaarmaClient(creds)

    if not client.probe_ro_scope():
        await _record(
            session, dealer_id, status="RO_SCOPE_NOT_GRANTED",
            detail={"endpoint": "order/v2/global_order", "reason": "ApiScope not provisioned"},
            ro_scope=False,
        )
        await session.commit()
        return SyncResult(
            False,
            "Order v2 (repair-order) scope not granted by myKaarma — RO data continues "
            "to come from the CSV import.",
            {"ro_scope_granted": False},
        )

    # Scope IS granted. Order v2 is read-by-UUID and myKaarma has not exposed an
    # enumeration endpoint (every list/search variant 404s), so a full pull needs
    # either that endpoint or webhooks. Until then we ingest the UUIDs we are
    # given — which is a fully working path, just not a self-driving one.
    if not order_uuids:
        await _record(
            session, dealer_id, status="RO_SCOPE_OK_NO_ENUMERATION",
            detail={"note": "scope granted; no order enumeration endpoint available"},
            ro_scope=True,
        )
        await session.commit()
        return SyncResult(
            True,
            "Order v2 scope IS granted and ingestion is ready. myKaarma exposes no order "
            "list/search endpoint, so pass order UUIDs to ingest them (or ask myKaarma to "
            "enable an enumeration endpoint / webhooks for an automatic pull).",
            {"ro_scope_granted": True, "ingested": 0, "needs_enumeration": True},
        )

    ingested, failed, drift = 0, [], []
    for ou in order_uuids:
        try:
            payload = client.get_order(ou)
        except MyKaarmaError as e:
            failed.append({"order_uuid": ou, "error": str(e)[:200]})
            continue
        try:
            result = await ingest_order(session, dealer_id, payload)
            ingested += 1
            if result.get("dms_mismatch"):
                drift.append(result["dms_mismatch"])
        except Exception as e:  # noqa: BLE001 — never let one bad order kill the sync
            failed.append({"order_uuid": ou, "error": f"mapping/ingest failed: {e}"[:200]})

    detail = {
        "ro_scope_granted": True,
        "ingested": ingested,
        "failed": failed,
        "dms_mismatches": drift,
    }
    await _record(session, dealer_id, status="RO_SYNC_OK", detail=detail, ro_scope=True)
    await session.commit()
    msg = f"Ingested {ingested} repair order(s) from myKaarma."
    if failed:
        msg += f" {len(failed)} failed."
    if drift:
        msg += f" {len(drift)} DMS assignment mismatch(es) flagged."
    return SyncResult(True, msg, detail)


def _dec(value: float) -> Decimal:
    """Float -> Decimal without inheriting binary float error.

    Decimal(1.2) captures 1.1999999999999999555910790149937383830547332763671875;
    Decimal("1.2") is exactly 1.2. Labor hours land in numeric columns, so go via
    str() to keep the stored value clean.
    """
    return Decimal(str(round(float(value or 0), 2)))


async def ingest_order(session: AsyncSession, dealer_id: uuid.UUID, payload: dict) -> dict:
    """Upsert one mapped Order v2 payload into repair_orders + ro_lines.

    Assignment drift (contract rule): myKaarma does NOT accept writes to
    DMS-originated orders, so our dispatch assignment is OUR state. If the DMS
    later reports a different tech on the line, we surface a mismatch rather
    than silently overwriting either side.
    """
    mapped = map_order(payload)
    if not mapped.ro_number:
        raise ValueError("order payload has no RO number")

    existing = (
        await session.execute(
            select(RepairOrder).where(
                RepairOrder.dealer_id == dealer_id,
                RepairOrder.ro_number == mapped.ro_number,
            )
        )
    ).scalar_one_or_none()

    ro = existing or RepairOrder(dealer_id=dealer_id, ro_number=mapped.ro_number)

    # Never clobber a locally dispatched RO's status with a stale DMS bucket.
    locally_dispatched = existing is not None and existing.status == "IN_PROGRESS"
    if not locally_dispatched:
        ro.status = mapped.status

    ro.vin = mapped.vin or ro.vin
    ro.vehicle_year = mapped.vehicle_year or ro.vehicle_year
    ro.vehicle_make = mapped.vehicle_make or ro.vehicle_make
    ro.vehicle_model = mapped.vehicle_model or ro.vehicle_model
    ro.mileage = mapped.mileage or ro.mileage
    ro.est_hours = _dec(mapped.est_hours) if mapped.est_hours else (ro.est_hours or 0)
    ro.written_at = mapped.written_at or ro.written_at
    ro.promise_at = mapped.promise_at or ro.promise_at
    ro.advisor_id = mapped.advisor_id or ro.advisor_id
    # preserve manually-set flags (MGR_FLAG, HEAT_CASE), merge in the DMS ones
    ro.flags = sorted(set(list(ro.flags or [])) | set(mapped.flags))

    # concern category / work type / required certs come from the dealer's
    # op-code map — the SM's mapping, not a guess from myKaarma.
    op_codes = [l.op_code for l in mapped.lines if l.op_code]
    if op_codes:
        mappings = {
            m.op_code: m
            for m in (
                await session.execute(
                    select(OpCodeMap).where(
                        OpCodeMap.dealer_id == dealer_id, OpCodeMap.op_code.in_(op_codes)
                    )
                )
            ).scalars()
        }
        primary = next((mappings.get(c) for c in op_codes if mappings.get(c)), None)
        if primary:
            ro.concern_category = primary.concern_category
            ro.work_type = primary.work_type or ro.work_type
            ro.tier = primary.tier or ro.tier
        else:
            ro.concern_category = ro.concern_category or "Uncategorised"

    if existing is None:
        session.add(ro)
    await session.flush()

    # replace lines with what the DMS currently says
    await session.execute(delete(ROLine).where(ROLine.ro_id == ro.id))
    for i, l in enumerate(mapped.lines):
        session.add(
            ROLine(
                dealer_id=dealer_id,
                ro_id=ro.id,
                op_code=l.op_code,
                description=l.description,
                flagged_hours=_dec(l.flagged_hours),
                sort_order=i,
            )
        )

    # --- drift detection -------------------------------------------------- #
    mismatch = None
    if mapped.dms_tech_nos:
        ours = (
            await session.execute(
                select(Assignment)
                .where(Assignment.dealer_id == dealer_id, Assignment.ro_id == ro.id)
                .order_by(Assignment.assigned_at.desc())
            )
        ).scalars().first()
        if ours is not None:
            tech = await session.get(Technician, ours.technician_id)
            our_no = (tech.dms_tech_no or "").strip() if tech else ""
            if our_no and our_no not in mapped.dms_tech_nos:
                mismatch = {
                    "ro_number": mapped.ro_number,
                    "ours": our_no,
                    "dms": mapped.dms_tech_nos,
                }

    return {"ro_number": mapped.ro_number, "dms_mismatch": mismatch}
