"""Esther dashboard ingestion trigger — hit by the scheduler (Vercel cron or
Railway cron) every 15 min to keep esther_daily_metrics current (real-time).

Secret-protected: pass the shared secret as ?secret= or the X-Cron-Secret header.
"""

from __future__ import annotations

import os
from datetime import date as date_cls

from fastapi import APIRouter, Header, HTTPException, Query

router = APIRouter(prefix="/esther", tags=["esther"])


def _check_secret(secret: str | None, x_cron_secret: str | None) -> None:
    expected = os.getenv("ESTHER_INGEST_SECRET") or os.getenv("CRON_SECRET")
    provided = secret or x_cron_secret
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/ingest")
async def ingest(
    days: int = Query(1, ge=1, le=60),
    secret: str | None = Query(None),
    x_cron_secret: str | None = Header(None),
):
    _check_secret(secret, x_cron_secret)
    # imported lazily so a missing script never blocks app startup
    from esther_ingest import run_ingest

    summary = await run_ingest(days, log=lambda *_: None)
    return {"ok": True, "days": days, "stores": summary}


@router.get("/daily-report")
async def daily_report_preview(
    date: str | None = Query(None, description="YYYY-MM-DD; defaults to today (CT)"),
    secret: str | None = Query(None),
    x_cron_secret: str | None = Header(None),
):
    """Preview the built report payloads (group + per store) without sending."""
    _check_secret(secret, x_cron_secret)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.db import engine
    from app.services.esther_daily_report import build_report_payloads

    d = date_cls.fromisoformat(date) if date else datetime.now(ZoneInfo("America/Chicago")).date()
    async with engine.begin() as conn:
        pg = (await conn.get_raw_connection()).driver_connection
        payloads = await build_report_payloads(pg, d)
    return {"date": d.isoformat(), "count": len(payloads), "payloads": payloads}


@router.post("/daily-report/send")
async def daily_report_send(
    date: str | None = Query(None),
    secret: str | None = Query(None),
    x_cron_secret: str | None = Header(None),
):
    """Build and POST the reports to the GHL inbound webhook now (manual trigger)."""
    _check_secret(secret, x_cron_secret)
    from app.services.esther_daily_report import send_daily_reports

    d = date_cls.fromisoformat(date) if date else None
    result = await send_daily_reports(d)
    # never echo the full payloads on send (keep the response small)
    result.pop("payloads", None)
    return {"ok": True, **result}
