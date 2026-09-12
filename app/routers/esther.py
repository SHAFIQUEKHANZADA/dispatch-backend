"""Esther dashboard ingestion trigger — hit by the scheduler (Vercel cron or
Railway cron) every 15 min to keep esther_daily_metrics current (real-time).

Secret-protected: pass the shared secret as ?secret= or the X-Cron-Secret header.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException, Query

router = APIRouter(prefix="/esther", tags=["esther"])


@router.post("/ingest")
async def ingest(
    days: int = Query(1, ge=1, le=60),
    secret: str | None = Query(None),
    x_cron_secret: str | None = Header(None),
):
    expected = os.getenv("ESTHER_INGEST_SECRET") or os.getenv("CRON_SECRET")
    provided = secret or x_cron_secret
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    # imported lazily so a missing script never blocks app startup
    from esther_ingest import run_ingest

    summary = await run_ingest(days, log=lambda *_: None)
    return {"ok": True, "days": days, "stores": summary}
