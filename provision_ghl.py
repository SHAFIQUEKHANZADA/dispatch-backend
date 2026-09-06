"""Provision per-store GoHighLevel credentials into the ghl_dealers table.

Applies migration 0007 (idempotent) and upserts one row per McGrath store,
matching each store by its stable dealer_key. Re-runnable: existing rows are
updated in place. Run from backend/:  python provision_ghl.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select, text

from app.db import SessionLocal, engine
from app.models import Dealer, GHLDealer

# Per-store GHL credentials live in an untracked file (never commit secrets).
# Copy ghl_credentials.example.json -> ghl_credentials.json and fill it in.
CREDENTIALS = Path(__file__).resolve().parent / "ghl_credentials.json"


def load_stores() -> dict[str, dict]:
    if not CREDENTIALS.exists():
        sys.exit(
            f"missing {CREDENTIALS.name} — copy ghl_credentials.example.json to it "
            "and fill in the real per-store values (see README/security notes)."
        )
    data = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    stores = {k: v for k, v in data.items() if not k.startswith("_")}
    for key, cfg in stores.items():
        if str(cfg.get("api_key", "")).startswith("pit-REPLACE"):
            sys.exit(f"{key}: api_key is still a placeholder — paste the rotated token first.")
    return stores


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
    stores = load_stores()
    async with SessionLocal() as session:
        for key, cfg in stores.items():
            api_key = cfg["api_key"]
            loc = cfg["location_id"]
            okey = cfg["object_key"]
            oid = cfg["object_id"]
            secret = cfg["webhook_secret"]
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
