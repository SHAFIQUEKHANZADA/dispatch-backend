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
    cors_origins: str = (
        "https://www.get3ddispatch.com, https://get3ddispatch.com, "
        "https://dispatch-kohl-pi.vercel.app, http://localhost:3000"
    )
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
    # The env creds above belong to exactly ONE store. This key names it, so the
    # env fallback is applied ONLY to that store and never leaks to other tenants.
    mykaarma_default_store_key: str = "mcgrath_honda_stcharles"

    # --- Warranty RO Audit (Claude reads the RO; the RESULT is deterministic) --
    # The Anthropic key powers the RO *reading*. Without it the module still
    # loads, but an audit returns a clear "key not configured" error rather than
    # a fabricated result (Guardian rule).
    anthropic_api_key: str = ""
    warranty_audit_model: str = "claude-sonnet-5"
    # Safety cap on the batch audit so one click can't fan out over hundreds of
    # ROs (and Claude calls). The endpoint logs when it truncates — never silent.
    warranty_batch_cap: int = 25

    # --- GoHighLevel sync (OPTIONAL) ----------------------------------------
    # If these are set, each audit result is mirrored to the GHL "Warranty RO
    # Audit" custom object so Don's existing GHL workflow stays in sync. Left
    # empty = sync disabled; the audit still works fully without it.
    ghl_api_key: str = ""
    ghl_location_id: str = ""
    ghl_object_key: str = "custom_objects.warranty_ro_audits"
    ghl_object_id: str = ""
    ghl_base: str = "https://services.leadconnectorhq.com"
    # Shared secret GHL sends with its upload webhook (in ?token= or an
    # X-Webhook-Secret header) so the public endpoint can't be hit by anyone.
    ghl_webhook_secret: str = ""

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def ghl_configured(self) -> bool:
        return bool(self.ghl_api_key and self.ghl_location_id)

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
