from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import Base, engine
from .engine.types import ENGINE_VERSION
from .routers import (
    dashboard,
    dealer,
    dev,
    dispatch,
    esther,
    imports,
    loaners,
    mykaarma,
    repair_orders,
    reports,
    route_sheet,
    scoreboard,
    stores,
    technicians,
    timeline,
    warranty,
)

settings = get_settings()
log = logging.getLogger("3d-dispatch")


async def _esther_autosync_loop(minutes: int):
    """Keep the Esther dashboard near-real-time: pull today's calls + appointments
    every `minutes`. Runs inside the always-on backend, so no external scheduler
    is needed. Each run is idempotent (upsert + window rebuild); failures are
    logged and never take the web server down."""
    from esther_ingest import run_ingest  # imported lazily; pulls in app.db etc.

    await asyncio.sleep(20)  # let the web server finish booting first
    while True:
        try:
            summary = await run_ingest(1, log=lambda *_: None)
            log.info("esther autosync: ok %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("esther autosync: run failed — will retry next cycle")
        await asyncio.sleep(minutes * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auth_mode == "dev":
        log.warning(
            "AUTH_MODE=dev — dealer isolation is NOT enforced by a JWT. "
            "This is for local development only. Never deploy with it."
        )

    if settings.is_sqlite:
        # The SQL in supabase/migrations is the source of truth for Postgres.
        # On SQLite (the zero-setup demo path) we create the tables from the
        # models so the app is runnable without standing up a database first.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    autosync = None
    if settings.esther_autosync_minutes > 0 and not settings.is_sqlite:
        log.info("esther autosync: enabled, every %s min", settings.esther_autosync_minutes)
        autosync = asyncio.create_task(_esther_autosync_loop(settings.esther_autosync_minutes))

    yield

    if autosync:
        autosync.cancel()
        try:
            await autosync
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="3D Dispatch API",
    version=ENGINE_VERSION,
    description=(
        "AI dispatch and performance system for dealership Fixed Operations.\n\n"
        "The Match Score is a deterministic weighted algorithm — never an LLM. "
        "Every score returns its reasons. No metric is computed when its source "
        "data is stale or missing."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # Backstop so a missing/wrong CORS_ORIGINS var can't silently break the app:
    # always allow the product domain (any subdomain) and Vercel preview URLs.
    allow_origin_regex=r"https://([a-z0-9-]+\.)*get3ddispatch\.com|https://[a-z0-9-]+\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (
    dealer.router,
    technicians.router,
    repair_orders.router,
    dispatch.router,
    imports.router,
    scoreboard.router,
    dashboard.router,
    mykaarma.router,
    loaners.router,
    timeline.router,
    reports.router,
    route_sheet.router,
    stores.router,
    warranty.router,
    esther.router,
    dev.router,
):
    app.include_router(r, prefix=settings.api_prefix)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "auth_mode": settings.auth_mode,
        "database": "sqlite" if settings.is_sqlite else "postgres",
    }
