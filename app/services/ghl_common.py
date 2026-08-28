"""Per-store GoHighLevel credentials.

Each McGrath store is its own GHL sub-account (Location) with its own Private
Integration Token, warranty custom object, and webhook secret. These live in the
`ghl_dealers` table, one row per store, so a Honda audit writes back into Honda's
GHL and a text sends from Honda's number — never crossing into Acura's account.

  * get_ghl_creds(session, dealer_id) — the warranty write-back and tech SMS look
    up the store they belong to.
  * ghl_creds_by_location(session, location_id) — the inbound webhook has no
    dealer context; it resolves the store by the GHL location id in the payload.

The env vars (settings.ghl_*) remain as a single-store fallback for the default
store only, so a not-yet-seeded environment still works during rollout.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import Dealer, GHLDealer

settings = get_settings()


@dataclass(frozen=True)
class GHLCreds:
    dealer_id: uuid.UUID
    api_key: str
    location_id: str
    object_key: str
    object_id: Optional[str] = None
    webhook_secret: Optional[str] = None

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Version": "2021-07-28",
            "content-type": "application/json",
        }


def _from_row(row: GHLDealer) -> GHLCreds:
    return GHLCreds(
        dealer_id=row.dealer_id,
        api_key=row.api_key,
        location_id=row.location_id,
        object_key=row.object_key or settings.ghl_object_key,
        object_id=row.object_id,
        webhook_secret=row.webhook_secret,
    )


async def _env_creds_dealer_id(session: AsyncSession) -> Optional[uuid.UUID]:
    """dealer_id of the store the env-var GHL creds belong to (the default store)."""
    row = (
        await session.execute(
            select(Dealer.id).where(Dealer.dealer_key == settings.ghl_default_store_key)
        )
    ).first()
    return row[0] if row else None


async def get_ghl_creds(session: AsyncSession, dealer_id: uuid.UUID) -> Optional[GHLCreds]:
    """Per-store GHL creds for this dealer, or None if the store has no GHL wired.
    Falls back to the env vars only for the single default store."""
    row = (
        await session.execute(
            select(GHLDealer).where(
                GHLDealer.dealer_id == dealer_id, GHLDealer.enabled.is_(True)
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return _from_row(row)

    # Env fallback — default store only, so a not-yet-seeded env still works.
    if settings.ghl_configured and dealer_id == await _env_creds_dealer_id(session):
        return GHLCreds(
            dealer_id=dealer_id,
            api_key=settings.ghl_api_key,
            location_id=settings.ghl_location_id,
            object_key=settings.ghl_object_key,
            object_id=settings.ghl_object_id or None,
            webhook_secret=settings.ghl_webhook_secret or None,
        )
    return None


async def ghl_creds_by_location(
    session: AsyncSession, location_id: str
) -> Optional[GHLCreds]:
    """Resolve a store's creds from the GHL location id (webhook routing)."""
    if not location_id:
        return None
    row = (
        await session.execute(
            select(GHLDealer).where(
                GHLDealer.location_id == location_id, GHLDealer.enabled.is_(True)
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return _from_row(row)
    # Env fallback for the default store's location.
    if settings.ghl_configured and location_id == settings.ghl_location_id:
        did = await _env_creds_dealer_id(session)
        if did is not None:
            return await get_ghl_creds(session, did)
    return None
