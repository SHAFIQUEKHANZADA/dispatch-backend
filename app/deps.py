"""Auth + tenant scoping.

NFR-1: dealer isolation is enforced at the DB (RLS) *and* here at the API layer.
Both, not either.  The backend connects with a service-role key that bypasses
RLS, so this file is the layer that actually holds the line in practice.

Every router depends on `CurrentUser`, and every query filters on
`current.dealer_id`.  There is no code path that reads a tenant table without
one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Optional

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session
from .models import Dealer, DealerSettings, UserProfile

settings = get_settings()

ROLE_RANK = {"DISPATCHER": 1, "SERVICE_MANAGER": 2, "ADMIN": 3}


@dataclass
class CurrentUser:
    user_id: Optional[uuid.UUID]
    dealer_id: uuid.UUID
    role: str

    def require_role(self, minimum: str) -> None:
        if ROLE_RANK.get(self.role, 0) < ROLE_RANK.get(minimum, 99):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This action requires the {minimum} role",
            )


async def _dev_user(session: AsyncSession, dealer_header: Optional[str]) -> CurrentUser:
    """Dev-only: no JWT.  Trusts a header, or falls back to the single dealer.

    Guarded by AUTH_MODE=dev.  Deploying with this set is a security incident,
    which is why the app logs a loud warning at startup when it sees it.
    """
    if dealer_header:
        try:
            dealer_id = uuid.UUID(dealer_header)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Dealer-Id is not a valid UUID")
    else:
        result = await session.execute(select(Dealer.id).limit(2))
        ids = [row[0] for row in result.all()]
        if not ids:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "No dealership exists yet. Run `python seed.py` to load the demo store.",
            )
        if len(ids) > 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "More than one dealership exists — send an X-Dealer-Id header to pick one.",
            )
        dealer_id = ids[0]

    return CurrentUser(user_id=None, dealer_id=dealer_id, role="ADMIN")


@lru_cache
def _jwks_client() -> "jwt.PyJWKClient":
    # Cached across requests; PyJWKClient caches the fetched signing keys too.
    jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    return jwt.PyJWKClient(jwks_url)


def _verify_supabase_jwt(token: str) -> dict:
    """Verify a Supabase access token.

    Supabase projects that have migrated to asymmetric signing keys issue
    ES256/RS256 tokens verified against the public JWKS endpoint (no shared
    secret). Older projects issue HS256 tokens verified with the legacy JWT
    secret. We branch on the token's own `alg` header so both work.
    """
    try:
        alg = jwt.get_unverified_header(token).get("alg", "")
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Malformed token: {exc}") from exc

    try:
        if alg in ("ES256", "RS256", "EdDSA"):
            if not settings.supabase_url:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "SUPABASE_URL is required to verify asymmetric (JWKS) tokens",
                )
            signing_key = _jwks_client().get_signing_key_from_jwt(token)
            return jwt.decode(
                token, signing_key.key, algorithms=[alg], audience="authenticated"
            )
        # legacy HS256 shared secret
        if not settings.supabase_jwt_secret:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Token is HS256 but SUPABASE_JWT_SECRET is not configured",
            )
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {exc}") from exc


async def _supabase_user(session: AsyncSession, authorization: Optional[str]) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1]

    claims = _verify_supabase_jwt(token)

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has no subject")

    user_id = uuid.UUID(sub)
    profile = await session.get(UserProfile, user_id)
    if profile is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This user is not attached to a dealership (no user_profiles row)",
        )

    return CurrentUser(user_id=user_id, dealer_id=profile.dealer_id, role=profile.role)


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[Optional[str], Header()] = None,
    x_dealer_id: Annotated[Optional[str], Header()] = None,
) -> CurrentUser:
    if settings.auth_mode == "dev":
        return await _dev_user(session, x_dealer_id)
    return await _supabase_user(session, authorization)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_dealer_settings(session: AsyncSession, dealer_id: uuid.UUID) -> DealerSettings:
    ds = await session.get(DealerSettings, dealer_id)
    if ds is None:
        ds = DealerSettings(dealer_id=dealer_id)
        session.add(ds)
        await session.flush()
    return ds
