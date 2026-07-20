from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ------------------------------------------------------------
    # Production (Supabase):
    #   postgresql+asyncpg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres
    # Local demo (zero setup):
    #   sqlite+aiosqlite:///./dispatch.db
    database_url: str = "sqlite+aiosqlite:///./dispatch.db"
    db_echo: bool = False

    # --- Auth ----------------------------------------------------------------
    # supabase : verify the Supabase JWT, resolve dealer_id from user_profiles.
    # dev      : trust an X-Dealer-Id header (or fall back to the only dealer).
    #            NEVER deploy with this.
    auth_mode: Literal["supabase", "dev"] = "dev"
    supabase_url: str = ""
    supabase_jwt_secret: str = ""
    supabase_service_role_key: str = ""

    # --- App -----------------------------------------------------------------
    cors_origins: str = "https://dispatch-kohl-pi.vercel.app, http://localhost:3000"
    api_prefix: str = "/api"

    # Demo clock.  When set (ISO 8601), the app scores "now" against this instant
    # instead of the wall clock, so the demo board is alive at any hour and the
    # Match Scores are reproducible.  seed.py writes this. Leave empty in prod.
    demo_now: str = ""

    # --- myKaarma (live DMS) ------------------------------------------------
    # Per-dealer credentials live in the mykaarma_dealers table. These env vars
    # are the fallback used for the sandbox / single-store setup. Rotate before
    # production. myKaarma geo-blocks non-US IPs on the web portal (not the API);
    # test the API from a US-hosted server (Railway).
    mykaarma_username: str = ""
    mykaarma_password: str = ""
    mykaarma_dealer_uuid: str = ""
    mykaarma_department_uuid: str = ""

    @property
    def mykaarma_env_configured(self) -> bool:
        return bool(
            self.mykaarma_username
            and self.mykaarma_password
            and self.mykaarma_dealer_uuid
            and self.mykaarma_department_uuid
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
