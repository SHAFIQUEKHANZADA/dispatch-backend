"""Provision per-store GoHighLevel credentials into the ghl_dealers table.

Applies migration 0007 (idempotent) and upserts one row per McGrath store,
matching each store by its stable dealer_key. Re-runnable: existing rows are
updated in place. Run from backend/:  python provision_ghl.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select, text

from app.db import SessionLocal, engine
from app.models import Dealer, GHLDealer

# dealer_key -> (api_key, location_id, object_key, object_id, webhook_secret)
STORES = {
    "mcgrath_honda_stcharles": (
        "pit-5183441d-5385-4349-bd43-7c4fbc6603a5",
        "HU18sX5xyiO7gIs3Bwyx",
        "custom_objects.warranty_ro_audits",
        "6a91a7d22414af40a33d5dda",
        "wh_mcgrath_stc_1636ab5a",
    ),
    "mcgrath_honda_elgin": (
        "pit-ee2d840e-bdda-4e21-897d-2245d3293c65",
        "tj3HUTFhDGeHiw2B8HG8",
        "custom_objects.warranty_ro_audits",
        "6a91a6bb3e5704f21003f3fa",
        "wh_mcgrath_elg_7cdac63d",
    ),
    "mcgrath_acura_libertyville": (
        "pit-bd78d619-9102-4e91-8f0f-691183cd15a9",
        "1Q3CnW3bXdJB6R8Iicm4",
        "custom_objects.warranty_ro_audits",
        "6a91a7f4590a4ebe7fa3c315",
        "wh_mcgrath_lib_39a1a06d",
    ),
    "mcgrath_acura_mortongrove": (
        "pit-d5cf12c5-1a97-422b-8e56-bd9d3a84c797",
        "xmT0rEWnHedqfBzpaXsb",
        "custom_objects.warranty_ro_audits",
        "6a8f277866d3883b0e1882ea",
        "wh_mcgrath_9f3k2p7q",
    ),
}

MIGRATION = Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "0007_ghl_dealers.sql"


async def main() -> None:
    # 1) apply the migration (create table if not exists + policies).
    # Strip full-line comments first so a splitting on ';' can't mistake the
    # header prose for SQL.
    raw = MIGRATION.read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in raw.splitlines() if not ln.strip().startswith("--")
    )
    async with engine.begin() as conn:
        for stmt in [s.strip() for s in code.split(";") if s.strip()]:
            await conn.execute(text(stmt))
    print(f"migration applied: {MIGRATION.name}")

    # 2) upsert one row per store
    async with SessionLocal() as session:
        for key, (api_key, loc, okey, oid, secret) in STORES.items():
            dealer = (
                await session.execute(select(Dealer).where(Dealer.dealer_key == key))
            ).scalar_one_or_none()
            if dealer is None:
                print(f"  SKIP {key}: no dealer with this key in the DB")
                continue
            row = await session.get(GHLDealer, dealer.id)
            if row is None:
                row = GHLDealer(dealer_id=dealer.id)
                session.add(row)
                verb = "created"
            else:
                verb = "updated"
            row.api_key = api_key
            row.location_id = loc
            row.object_key = okey
            row.object_id = oid
            row.webhook_secret = secret
            row.enabled = True
            print(f"  {verb} {key} -> location {loc}")
        await session.commit()

    # 3) report
    async with SessionLocal() as session:
        rows = list((await session.execute(select(GHLDealer))).scalars())
        print(f"\nghl_dealers now holds {len(rows)} store(s).")


if __name__ == "__main__":
    asyncio.run(main())
