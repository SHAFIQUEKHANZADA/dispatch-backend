from __future__ import annotations

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

    yield


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
