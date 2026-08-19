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
    Dealer,
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
    """Each store uses its OWN myKaarma credentials — this is the multi-tenant
    boundary. Resolution order:
      1. the store's row in mykaarma_dealers (per-store creds), else
      2. the env creds — but ONLY for the single designated default store
         (settings.mykaarma_default_store_key), so one store's credentials can
         NEVER be used to pull another store's data.
    Returns None when the store has no credentials configured.
    """
    row = await session.get(MyKaarmaDealer, dealer_id)
    if row and row.enabled:
        return MyKaarmaCreds(
            username=row.username,
            password=row.password,
            dealer_uuid=row.dealer_uuid,
            department_uuid=row.department_uuid,
        )
    if settings.mykaarma_env_configured:
        dealer = await session.get(Dealer, dealer_id)
        if dealer and dealer.dealer_key == settings.mykaarma_default_store_key:
            return MyKaarmaCreds(
                username=settings.mykaarma_username,
                password=settings.mykaarma_password,
                dealer_uuid=settings.mykaarma_dealer_uuid,
                department_uuid=settings.mykaarma_department_uuid,
            )
    return None


async def resolve_creds_by_key(session: AsyncSession, dealer_key: str) -> Optional[MyKaarmaCreds]:
    """Look up a store's myKaarma creds by its dealer_key (store_id)."""
    dealer = (
        await session.execute(select(Dealer).where(Dealer.dealer_key == dealer_key))
    ).scalar_one_or_none()
    if dealer is None:
        return None
    return await resolve_creds(session, dealer.id)


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

    # 2. repair-order access. Probe BOTH documented scopes:
    #    * order.specific.search  -> lists/enumerates open ROs (specificSearch)
    #    * order read-by-UUID      -> full order detail (global_order)
    #    When search is granted, also report how many OPEN ROs the department has
    #    right now, so the owner sees the live number he can pull onto the board.
    try:
        search_granted = client.probe_search_scope()
        read_granted = client.probe_ro_scope()
        out["ro_search_scope_granted"] = search_granted
        out["ro_scope_granted"] = search_granted or read_granted  # any RO access

        open_count = None
        if search_granted:
            try:
                data = client.search_orders(order_status="O", order_type="RO", size=1)
                open_count = data.get("totalCount") or 0
                out["open_ro_count"] = open_count
            except MyKaarmaError:
                pass

        if search_granted:
            if open_count is not None:
                out["message"] = (
                    f"Connected. Repair-order scopes granted — {open_count} open RO(s) "
                    f"available to pull from myKaarma."
                    if open_count
                    else "Connected. Repair-order scopes granted — this department currently "
                    "has 0 open ROs in myKaarma (nothing to pull yet)."
                )
            else:
                out["message"] = "Connected. Repair-order search scope granted."
        elif read_granted:
            out["message"] = (
                "Connected. Order read scope granted, but the order-search scope "
                "(order.specific.search) is not — open ROs can't be auto-enumerated yet."
            )
        else:
            out["message"] = (
                "Connected, but the Order v2 repair-order scopes are NOT provisioned for "
                "this account yet — RO data falls back to CSV import."
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


def list_open_ro_uuids(client: MyKaarmaClient, max_pages: int = 20) -> tuple[list[str], int]:
    """Enumerate OPEN repair orders via the documented specificSearch endpoint.

    Returns (order_uuids, total_count). Pages through the results (size 150) so a
    busy shop's whole open board is captured, not just the first page.
    """
    uuids: list[str] = []
    total = 0
    page = 0
    while page < max_pages:
        data = client.search_orders(order_status="O", order_type="RO", page_no=page, size=150)
        total = data.get("totalCount") or 0
        orders = data.get("orders") or []
        if not orders:
            break
        for o in orders:
            ou = o.get("orderUuid") or o.get("orderUUID")
            if ou:
                uuids.append(ou)
        if len(uuids) >= total or len(orders) < 150:
            break
        page += 1
    return uuids, total


async def sync_repair_orders(
    session: AsyncSession,
    dealer_id: uuid.UUID,
    order_uuids: Optional[list[str]] = None,
) -> SyncResult:
    """Pull OPEN repair orders from myKaarma and ingest them onto the board.

    The proper flow (per the myKaarma Orders docs):
      1. ENUMERATE open ROs via POST order/v2/.../order/specificSearch
         (orderStatus=O, orderType=RO) — the documented list endpoint.
      2. For each order UUID, READ the full order via GET global_order/{uuid}
         (header + jobs[] + parts) and upsert it into repair_orders + ro_lines.

    Passing `order_uuids` explicitly skips enumeration (e.g. a webhook handing us
    one order). We never invent orders — an empty department ingests nothing.
    """
    creds = await resolve_creds(session, dealer_id)
    if creds is None:
        return SyncResult(False, "No myKaarma credentials for this dealer.", {})

    client = MyKaarmaClient(creds)

    # --- 1. determine the set of orders to ingest --------------------------- #
    total_open = None
    if order_uuids is None:
        if not client.probe_search_scope():
            # search scope missing — fall back to read-by-UUID if THAT is granted
            if not client.probe_ro_scope():
                await _record(
                    session, dealer_id, status="RO_SCOPE_NOT_GRANTED",
                    detail={"reason": "neither order.specific.search nor order read scope provisioned"},
                    ro_scope=False,
                )
                await session.commit()
                return SyncResult(
                    False,
                    "myKaarma has not provisioned the repair-order scopes for this account — "
                    "RO data continues to come from the CSV import.",
                    {"ro_scope_granted": False},
                )
            await _record(
                session, dealer_id, status="RO_SEARCH_SCOPE_MISSING", ro_scope=True,
                detail={"note": "read scope granted but order.specific.search is not — pass UUIDs to ingest"},
            )
            await session.commit()
            return SyncResult(
                True,
                "Read scope is granted but the order-search scope is not, so open ROs can't be "
                "enumerated automatically. Ask myKaarma to grant 'order.specific.search', or pass "
                "order UUIDs to ingest them directly.",
                {"ro_scope_granted": True, "search_scope": False, "ingested": 0},
            )
        try:
            order_uuids, total_open = list_open_ro_uuids(client)
        except MyKaarmaError as e:
            await _record(session, dealer_id, status="RO_ENUM_FAILED",
                          detail={"error": str(e)[:200]}, ro_scope=True)
            await session.commit()
            return SyncResult(False, f"Open-RO enumeration failed: {e}", {"ro_scope_granted": True})

    # --- 2. read + ingest each order ---------------------------------------- #
    if not order_uuids:
        await _record(
            session, dealer_id, status="RO_SYNC_OK", ro_scope=True,
            detail={"open_orders": total_open or 0, "ingested": 0},
        )
        await session.commit()
        return SyncResult(
            True,
            f"Connected to myKaarma and the repair-order scopes are granted. This department "
            f"currently reports {total_open or 0} open repair order(s) — nothing to ingest.",
            {"ro_scope_granted": True, "search_scope": True, "open_orders": total_open or 0, "ingested": 0},
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
        "search_scope": True,
        "open_orders": total_open,
        "ingested": ingested,
        "failed": failed,
        "dms_mismatches": drift,
    }
    await _record(session, dealer_id, status="RO_SYNC_OK", detail=detail, ro_scope=True)
    await session.commit()
    msg = f"Ingested {ingested} open repair order(s) from myKaarma."
    if failed:
        msg += f" {len(failed)} failed."
    if drift:
        msg += f" {len(drift)} DMS assignment mismatch(es) flagged."
    return SyncResult(True, msg, detail)


def _map_appointment(a: dict) -> dict:
    """One myKaarma serviceAppointment -> the shape the Upcoming ROs tab renders."""
    cust = a.get("customerInformation") or {}
    veh = a.get("vehicleInformation") or {}
    order = a.get("orderInformation") or {}
    name = f"{cust.get('firstName') or ''} {cust.get('lastName') or ''}".strip() or "Customer"

    # Only build a vehicle string if a real make/model was chosen — otherwise the
    # customer booked without selecting one, so show "Vehicle TBD" (not a bare year).
    has_real_vehicle = (veh.get("brand") not in (None, "", "Other")) or (
        veh.get("model") not in (None, "", "No Vehicle Selected")
    )
    if has_real_vehicle:
        veh_parts = [str(veh[k]) for k in ("year", "brand", "model")
                     if veh.get(k) and str(veh[k]) not in ("Other", "No Vehicle Selected", "trim")]
        vehicle = " ".join(veh_parts) or "Vehicle TBD"
    else:
        vehicle = "Vehicle TBD"

    transport = (a.get("transportOption") or {}).get("altTransportation")

    # Op-code line items the customer booked (the real DMS operations).
    services = []
    for sv in a.get("serviceList") or []:
        if not isinstance(sv, dict):
            continue
        services.append({
            "op_code": sv.get("laborOpCode") or sv.get("opCode"),
            "description": sv.get("description") or sv.get("opCodeName") or sv.get("name"),
            "duration_mins": sv.get("durationInMins"),
            "price": sv.get("price"),
            "pay_type": (sv.get("payType") or "").strip() or None,
            "operation_type": sv.get("operationType"),
        })

    comments = (a.get("comments") or "").strip()
    if services:
        service_requested = ", ".join(s["description"] for s in services if s.get("description"))
    elif comments.lower().startswith("service requested"):
        service_requested = comments.split(":", 1)[-1].split("|")[0].strip() or None
    else:
        service_requested = comments or None

    comm = a.get("appointmentCommunicationPreferences") or {}
    trim = veh.get("trim")
    return {
        "appointment_uuid": a.get("uuid"),
        "customer_uuid": cust.get("uuid"),
        "customer_name": name,
        "company": cust.get("company") or None,
        # appointment-level confirmation contact (often null); enriched below from
        # the customer record when missing.
        "phone": cust.get("confirmationPhone") or comm.get("confirmationPhoneNumber"),
        "email": cust.get("confirmationEmail") or comm.get("confirmationEmail"),
        "vehicle": vehicle,
        "vehicle_uuid": veh.get("uuid"),
        "vin": veh.get("vin"),
        "license_plate": veh.get("licensePlate"),
        "mileage": veh.get("mileage") or a.get("mileageText"),
        "color": veh.get("color"),
        "engine": veh.get("engine"),
        "trim": trim if trim not in (None, "", "trim") else None,
        "start_time": a.get("startTime"),        # "2026-07-28 10:00:00"
        "end_time": a.get("endTime"),
        "preferred_date": a.get("preferredDate"),
        "status": a.get("newStatus") or a.get("status"),
        "transport": transport,
        "service_requested": service_requested,
        "services": services,
        "internal_notes": (a.get("internalNotes") or "").strip() or None,
        "recall": bool(a.get("recall")),
        "source": a.get("appointmentSource") or a.get("platform"),
        "text_reminder": bool(comm.get("textReminder")),
        "advisor_uuid": a.get("assignedAdvisorUuid"),
        "order_number": order.get("orderNumber"),
        "has_order": bool(order.get("uuid")),
        "booked_at": a.get("date"),              # when the appointment was created
    }


async def upcoming_appointments(
    session: AsyncSession, dealer_id: uuid.UUID, days: int = 14, enrich: bool = True
) -> dict:
    """Booked appointments from today through +`days` (the voice-agent bookings).

    Reads the myKaarma Scheduler day-by-day (appointment.get) across the window,
    concurrently, and returns them sorted by start time. These are "upcoming ROs"
    — a booked appointment becomes a repair order when the customer checks in.
    """
    import asyncio
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from ..clock import default_now
    from ..models import Dealer

    creds = await resolve_creds(session, dealer_id)
    if creds is None:
        return {"available": False, "reason": "No myKaarma credentials for this dealer.", "appointments": []}

    client = MyKaarmaClient(creds)
    if not client.probe_appointment_scope():
        return {
            "available": False,
            "reason": "myKaarma has not granted the appointment.get scope for this account.",
            "appointments": [],
        }

    dealer = await session.get(Dealer, dealer_id)
    tz = ZoneInfo(dealer.timezone) if dealer and dealer.timezone else ZoneInfo("America/Chicago")
    today = default_now().astimezone(tz).date()
    dates = [(today + timedelta(days=n)).isoformat() for n in range(max(1, days) + 1)]

    def fetch(d: str):
        try:
            return client.get_appointments(d).get("serviceAppointments") or []
        except MyKaarmaError:
            return []

    # day queries run concurrently in threads (the client is sync httpx)
    per_day = await asyncio.gather(*[asyncio.to_thread(fetch, d) for d in dates])

    appts: list[dict] = []
    for day in per_day:
        for a in day:
            appts.append(_map_appointment(a))

    # Resolve each appointment's OWN customer record (read-only listMinimal) to
    # get real phone/email + the customer's real vehicle. One search per unique
    # customer, run concurrently, disambiguated back to the exact customer UUID.
    by_uuid: dict[str, str] = {}
    for a in appts:
        cu = a.get("customer_uuid")
        if cu and cu not in by_uuid:
            by_uuid[cu] = a.get("customer_name") or ""

    def lookup(cu: str, name: str):
        try:
            term = (name.split()[0] if name else "") or name
            matches = client.search_customers(term)
            # exact match on the appointment's customer UUID (handles same-name dupes)
            cust = next((m for m in matches if m.get("uuid") == cu), None)
            return cu, cust
        except MyKaarmaError:
            return cu, None

    # Per-customer enrichment is O(appointments) myKaarma calls — fine for a
    # handful, fatal for a busy store (1,000+ bookings => the request hangs). The
    # board passes enrich=False; the list already has customer/vehicle/concern.
    if enrich and by_uuid:
        pairs = await asyncio.gather(*[asyncio.to_thread(lookup, cu, nm) for cu, nm in by_uuid.items()])
        records = {cu: cust for cu, cust in pairs if cust}
        for a in appts:
            cust = records.get(a.get("customer_uuid"))
            if not cust:
                continue
            phone, email = _contact_from_comms(cust)
            a["phone"] = a.get("phone") or phone
            a["email"] = a.get("email") or email
            # the customer's real vehicle, matched to this appointment
            v = _pick_vehicle(cust, a.get("vehicle_uuid"))
            if v:
                yr, mk, md = v.get("year"), v.get("make"), v.get("model")
                a["vehicle"] = " ".join(str(x) for x in (yr, mk, md) if x) or a["vehicle"]
                a["vin"] = v.get("vin") or a.get("vin")

    appts.sort(key=lambda x: x.get("start_time") or "")
    return {
        "available": True,
        "count": len(appts),
        "window_days": days,
        "appointments": appts,
    }


def _is_junk_vehicle(v: dict) -> bool:
    return (v.get("model") == "No Vehicle Selected") or (v.get("make") == "Other")


def _pick_vehicle(cust: dict, appt_vehicle_uuid: Optional[str]) -> Optional[dict]:
    """The customer's real vehicle for this appointment: match the appointment's
    vehicle UUID (unless it points at a junk placeholder), else the newest valid
    vehicle. Junk 'No Vehicle Selected' / 'Other' entries are never returned."""
    valid = [v for v in (cust.get("vehicles") or []) if not _is_junk_vehicle(v)]
    if not valid:
        return None
    if appt_vehicle_uuid:
        for v in valid:
            if v.get("uuid") == appt_vehicle_uuid:
                return v
    return sorted(valid, key=lambda v: str(v.get("year") or ""), reverse=True)[0]


def _contact_from_comms(cust: dict) -> tuple[Optional[str], Optional[str]]:
    """First phone + email from a listMinimal customer's communications[]."""
    phone = email = None
    for cm in cust.get("communications") or []:
        val = (cm.get("commValue") or "").strip()
        if not val:
            continue
        if "@" in val:
            email = email or val
        elif (cm.get("commType") or "").upper() == "P" or val.replace("+", "").isdigit():
            phone = phone or val
    return phone, email


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
