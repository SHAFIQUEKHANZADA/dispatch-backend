"""Esther dashboard ingestion — pulls REAL data into the esther_* tables.

Sources (both proven working):
  * GHL conversations + AI workflow tags  -> esther_calls
  * myKaarma appointments (via connector) -> esther_appointments
Then rolls both up into esther_daily_metrics (what the dashboard reads).

Writes go straight to the shared Supabase Postgres (same DB the dispatch app
uses) via the backend engine, so it bypasses RLS as the DB owner. GHL tokens are
read from ai_dashboard/.env.local (gitignored) — never hardcoded here.

Run:  python esther_ingest.py [days]        # default 14
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.db import engine
from app.mykaarma.connector import resolve_creds, _map_appointment
from app.mykaarma.client import MyKaarmaClient, MyKaarmaError

GHL_BASE = "https://services.leadconnectorhq.com"
ENV_PATH = Path(__file__).resolve().parents[1] / "ai_dashboard" / ".env.local"

BOOKED_TAGS = {"service-booked", "sales-booked", "call-booked-ai"}
TRANSFER_TAGS = {"transferred", "call-transferred"}
FAILED_CALL_STATUS = {"no-answer", "busy", "failed", "voicemail", "canceled", "cancelled"}


def load_ghl_tokens() -> dict[str, str]:
    """GHL per-store tokens, keyed by store key. Prefers env vars (production /
    Railway) and falls back to ai_dashboard/.env.local for local dev."""
    import os
    tokens: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.startswith("GHL_TOKEN__") and v:
            tokens[k.replace("GHL_TOKEN__", "").strip()] = v.strip()
    if not tokens and ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("GHL_TOKEN__") and "=" in s:
                k, v = s.split("=", 1)
                tokens[k.replace("GHL_TOKEN__", "").strip()] = v.strip()
    return tokens


# ── GHL calls ─────────────────────────────────────────────
def fetch_call_conversations(location_id: str, token: str, since_ms: int) -> list[dict]:
    """Page conversations newest->oldest until older than `since_ms`."""
    H = {"Authorization": f"Bearer {token}", "Version": "2021-04-15", "Accept": "application/json"}
    out: list[dict] = []
    cursor: int | None = None
    with httpx.Client(timeout=30) as client:
        while True:
            params = {"locationId": location_id, "limit": 100, "sortBy": "last_message_date", "sort": "desc"}
            if cursor:
                params["startAfterDate"] = cursor
            r = client.get(f"{GHL_BASE}/conversations/search", params=params, headers=H)
            if r.status_code != 200:
                break
            cs = r.json().get("conversations") or []
            if not cs:
                break
            reached_end = False
            for c in cs:
                lmd = c.get("lastMessageDate")
                if not lmd:
                    continue
                if lmd < since_ms:
                    reached_end = True
                    continue
                out.append(c)
            cursor = cs[-1].get("lastMessageDate")
            if reached_end or len(cs) < 100 or not cursor:
                break
    return out


def is_call(c: dict) -> bool:
    return (
        c.get("type") == "TYPE_PHONE"
        or c.get("lastMessageType") == "TYPE_CALL"
        or "TYPE_CALL" in (c.get("messageTypes") or [])
    )


def fetch_contacts(location_id: str, token: str, since_ms: int) -> list[dict]:
    """Contacts CREATED since `since_ms` — this matches how GHL's own dashboard
    counts (records by dateAdded + tags), so our numbers reconcile with theirs.
    Each contact Esther handled carries the AI workflow's tags."""
    H = {
        "Authorization": f"Bearer {token}", "Version": "2021-07-28",
        "Accept": "application/json", "Content-Type": "application/json",
    }
    out: list[dict] = []
    search_after = None
    with httpx.Client(timeout=30) as client:
        while True:
            body: dict = {
                "locationId": location_id,
                "pageLimit": 100,
                "filters": [{"field": "dateAdded", "operator": "range", "value": {"gte": since_ms}}],
                "sort": [{"field": "dateAdded", "direction": "desc"}],
            }
            if search_after:
                body["searchAfter"] = search_after
            r = client.post(f"{GHL_BASE}/contacts/search", headers=H, json=body)
            if r.status_code != 200:
                break
            cs = r.json().get("contacts") or []
            if not cs:
                break
            out.extend(cs)
            search_after = cs[-1].get("searchAfter")
            if len(cs) < 100 or not search_after:
                break
    return out


def _parse_dt(v) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def derive_from_contact(c: dict, tz: ZoneInfo) -> dict:
    tags = [str(t).lower() for t in (c.get("tags") or [])]
    tagset = set(tags)
    dept = "service" if "dept-service" in tagset else ("sales" if "dept-sales" in tagset else None)
    transferred = bool(tagset & TRANSFER_TAGS)
    callback = "callback-needed" in tagset
    if tagset & BOOKED_TAGS:
        outcome = "booked"
    elif "dropped" in tagset:
        outcome = "dropped"
    elif callback:
        outcome = "callback_needed"
    elif "info-only" in tagset:
        outcome = "info_only"
    elif "no-transcript" in tagset:
        outcome = "no_transcript"
    else:
        outcome = None
    intent = next((t[len("topic-"):] for t in tags if t.startswith("topic-")), None)
    started = _parse_dt(c.get("dateAdded"))
    ld = started.astimezone(tz).date()
    return {
        "ghl_conversation_id": None,
        "ghl_contact_id": c.get("id"),
        "ghl_message_id": c.get("id"),  # one row per contact
        "started_at": started,
        "local_date": ld,
        "direction": "inbound",
        "department": dept,
        "outcome": outcome,
        "intent": intent,
        "transferred": transferred,
        "transfer_succeeded": None,
        "callback_needed": callback,
        "needs_attention": "needs-attention" in tagset,
        "tags": tags,
    }


def derive_call(c: dict, tz: ZoneInfo) -> dict:
    tags = [str(t).lower() for t in (c.get("tags") or [])]
    tagset = set(tags)
    dept = "service" if "dept-service" in tagset else ("sales" if "dept-sales" in tagset else None)
    transferred = bool(tagset & TRANSFER_TAGS)
    callback = "callback-needed" in tagset
    if tagset & BOOKED_TAGS:
        outcome = "booked"
    elif "dropped" in tagset:
        outcome = "dropped"
    elif callback:
        outcome = "callback_needed"
    elif "info-only" in tagset:
        outcome = "info_only"
    elif "no-transcript" in tagset:
        outcome = "no_transcript"
    else:
        outcome = None
    intent = next((t[len("topic-"):] for t in tags if t.startswith("topic-")), None)
    call_status = str(c.get("lastCallStatus") or "").lower()
    transfer_succeeded = None
    if transferred:
        transfer_succeeded = call_status not in FAILED_CALL_STATUS if call_status else None
    ms = c.get("lastMessageDate")
    started = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    ld = started.astimezone(tz).date()
    return {
        "ghl_conversation_id": c.get("id"),
        "ghl_contact_id": c.get("contactId"),
        "ghl_message_id": f"{c.get('id')}:{ld.isoformat()}",
        "started_at": started,
        "local_date": ld,
        "direction": (c.get("lastMessageDirection") or None),
        "department": dept,
        "outcome": outcome,
        "intent": intent,
        "transferred": transferred,
        "transfer_succeeded": transfer_succeeded,
        "callback_needed": callback,
        "needs_attention": "needs-attention" in tagset,
        "tags": tags,
    }


async def sync_calls(pg, store: dict, since_ms: int, tokens: dict[str, str]) -> int:
    loc = store["ghl_location_id"]
    token = tokens.get(store["key"])
    if not loc or not token:
        return 0
    tz = ZoneInfo(store["timezone"] or "America/Chicago")
    contacts = fetch_contacts(loc, token, since_ms)
    rows = [derive_from_contact(c, tz) for c in contacts]
    if not rows:
        return 0
    params = [
        (
            store["id"], r["ghl_conversation_id"], r["ghl_contact_id"], r["ghl_message_id"],
            r["started_at"], r["local_date"], r["direction"], r["department"], r["outcome"],
            r["intent"], r["transferred"], r["transfer_succeeded"], r["callback_needed"],
            r["needs_attention"], r["tags"],
        )
        for r in rows
    ]
    await pg.executemany(
        """
        insert into esther_calls
          (store_id, ghl_conversation_id, ghl_contact_id, ghl_message_id, started_at,
           local_date, direction, department, outcome, intent, transferred,
           transfer_succeeded, callback_needed, needs_attention, tags)
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        on conflict (ghl_message_id) do update set
           outcome=excluded.outcome, intent=excluded.intent, department=excluded.department,
           transferred=excluded.transferred, transfer_succeeded=excluded.transfer_succeeded,
           callback_needed=excluded.callback_needed, needs_attention=excluded.needs_attention,
           tags=excluded.tags, started_at=excluded.started_at
        """,
        params,
    )
    return len(rows)


# ── myKaarma appointments ─────────────────────────────────
SOURCE_MAP = {"Appointment API": "ai", "DMS": "dms", "Online Scheduler": "online"}


def map_source(raw: str | None) -> str:
    return SOURCE_MAP.get(raw or "", "online")


async def dispatch_dealer_id(pg, dealer_key: str) -> uuid.UUID | None:
    row = await pg.fetchrow("select id from dealers where dealer_key=$1", dealer_key)
    return row["id"] if row else None


async def sync_appointments(pg, session_factory, store: dict, day_lo, day_hi, tz: ZoneInfo) -> int:
    """Pull appointments whose SERVICE date is in [day_lo, day_hi+45d]; store each,
    dating the row by its BOOKING date (when Esther booked it)."""
    from app.db import SessionLocal
    dealer_key = store["mykaarma_dealer_key"]
    if not dealer_key:
        return 0
    async with SessionLocal() as session:
        did = await dispatch_dealer_id(pg, dealer_key)
        if did is None:
            return 0
        creds = await resolve_creds(session, did)
        if creds is None:
            return 0
    client = MyKaarmaClient(creds)
    try:
        if not client.probe_appointment_scope():
            return 0
    except Exception:
        return 0
    # scan service days (booking date <= service date), a bit before and well after
    scan_lo = day_lo
    scan_hi = day_hi + timedelta(days=45)
    days = [(scan_lo + timedelta(n)).isoformat() for n in range((scan_hi - scan_lo).days + 1)]

    def fetch(d: str):
        try:
            return client.get_appointments(d).get("serviceAppointments") or []
        except MyKaarmaError:
            return []

    # fetch all service-days concurrently (was the slow part)
    per_day = await asyncio.gather(*[asyncio.to_thread(fetch, d) for d in days])
    params = []
    seen = set()
    for day in per_day:
        for a in day:
            m = _map_appointment(a)
            booked_raw = m.get("booked_at")
            start_raw = m.get("start_time")
            try:
                booked_dt = datetime.fromisoformat((booked_raw or "").replace(" ", "T"))
                if booked_dt.tzinfo is None:
                    booked_dt = booked_dt.replace(tzinfo=timezone.utc)
            except Exception:
                booked_dt = None
            try:
                start_dt = datetime.fromisoformat((start_raw or "").replace(" ", "T"))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            except Exception:
                start_dt = None
            # date the row by BOOKING day (Esther booked it that day)
            basis = booked_dt or start_dt
            if basis is None:
                continue
            local_date = basis.astimezone(tz).date()
            if not (day_lo <= local_date <= day_hi):
                continue
            appt_uuid = m.get("appointment_uuid")
            if not appt_uuid or appt_uuid in seen:
                continue  # de-dupe (same appointment can surface across scanned days)
            seen.add(appt_uuid)
            params.append((
                store["id"], appt_uuid, m.get("customer_name"),
                m.get("vehicle"), m.get("service_requested"),
                start_dt or basis, local_date, map_source(m.get("source")),
            ))
    if not params:
        return 0
    await pg.executemany(
        """
        insert into esther_appointments
          (store_id, mykaarma_appointment_uuid, customer_name, vehicle, service,
           start_time, local_date, source)
        values ($1,$2,$3,$4,$5,$6,$7,$8)
        on conflict (mykaarma_appointment_uuid) do update set
           local_date=excluded.local_date, source=excluded.source,
           start_time=excluded.start_time, customer_name=excluded.customer_name
        """,
        params,
    )
    return len(params)


# ── Rollup ────────────────────────────────────────────────
async def rollup(pg, store_id, date: str) -> None:
    await pg.execute(
        """
        with c as (
          select * from esther_calls where store_id=$1 and local_date=$2
        ), a as (
          select * from esther_appointments where store_id=$1 and local_date=$2
        ), intent as (
          -- count EVERY call; calls with no topic tag go to 'uncategorized' so the
          -- Customer Intent donut totals to total_calls (no silently-dropped calls).
          select coalesce(nullif(trim(intent),''),'uncategorized') k, count(*) n from c group by 1
        ), sp as (
          -- real AI spend for the day, from the GHL billing export (esther_ai_spend).
          -- null when nothing imported yet, so the card shows "Awaiting data", never $0.
          select sum(amount_usd) spend from esther_ai_spend
          where store_id=$1 and local_date=$2
            and source in ('voice_ai','workflow_ai','reviews_ai','conversation_ai','content_ai')
        )
        insert into esther_daily_metrics as dm
          (store_id, local_date, total_calls, appointments_booked, eligible_calls, booking_pct,
           transfers, failed_transfers, dropped_calls, callbacks_needed, intent_breakdown,
           ai_spend, cost_per_booking, updated_at)
        select
          $1, $2,
          (select count(*) from c),
          (select count(*) from c where outcome='booked'),
          (select count(*) from c where department in ('service','sales') and coalesce(outcome,'')<>'no_transcript'),
          case when (select count(*) from c where department in ('service','sales') and coalesce(outcome,'')<>'no_transcript') > 0
               then round(100.0 * (select count(*) from c where outcome='booked')
                    / (select count(*) from c where department in ('service','sales') and coalesce(outcome,'')<>'no_transcript'), 2)
               else null end,
          (select count(*) from c where transferred),
          (select count(*) from c where transferred and transfer_succeeded is false),
          (select count(*) from c where outcome='dropped'),
          (select count(*) from c where callback_needed),
          coalesce((select jsonb_object_agg(k, n) from intent), '{}'::jsonb),
          (select spend from sp),
          case when (select spend from sp) is not null and (select count(*) from c where outcome='booked') > 0
               then round((select spend from sp) / (select count(*) from c where outcome='booked'), 2)
               else null end,
          now()
        on conflict (store_id, local_date) do update set
          total_calls=excluded.total_calls, appointments_booked=excluded.appointments_booked,
          eligible_calls=excluded.eligible_calls, booking_pct=excluded.booking_pct,
          transfers=excluded.transfers, failed_transfers=excluded.failed_transfers,
          dropped_calls=excluded.dropped_calls, callbacks_needed=excluded.callbacks_needed,
          intent_breakdown=excluded.intent_breakdown,
          ai_spend=excluded.ai_spend, cost_per_booking=excluded.cost_per_booking, updated_at=now()
        """,
        store_id, date,
    )


async def run_ingest(days: int, log=print) -> list[dict]:
    """Ingest the last `days` days for every active store. Returns a per-store
    summary. Does NOT dispose the engine (safe to call from a running server)."""
    tokens = load_ghl_tokens()
    tz_default = ZoneInfo("America/Chicago")
    today = datetime.now(tz_default).date()
    day_lo = today - timedelta(days=days - 1)
    since_ms = int(datetime(day_lo.year, day_lo.month, day_lo.day, tzinfo=tz_default).timestamp() * 1000)

    summary: list[dict] = []
    # read the store list first (own txn)
    async with engine.begin() as conn:
        pg = (await conn.get_raw_connection()).driver_connection
        stores = [
            dict(s)
            for s in await pg.fetch(
                "select id, key, name, ghl_location_id, mykaarma_dealer_key, timezone "
                "from esther_stores where active order by sort_order"
            )
        ]
    # each store commits in its OWN txn, so its data appears as soon as it's done
    for store in stores:
        tz = ZoneInfo(store["timezone"] or "America/Chicago")
        async with engine.begin() as conn:
            pg = (await conn.get_raw_connection()).driver_connection
            nc = await sync_calls(pg, store, since_ms, tokens)
            na = await sync_appointments(pg, None, store, day_lo, today, tz)
            for n in range(days):
                await rollup(pg, store["id"], day_lo + timedelta(n))
        summary.append({"store": store["name"], "calls": nc, "appointments": na})
        log(f"{store['name']:<32} calls={nc:<5} appts={na:<5} (rolled {days} days)")
    return summary


async def main(days: int) -> None:
    await run_ingest(days)
    await engine.dispose()


if __name__ == "__main__":
    d = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    asyncio.run(main(d))
