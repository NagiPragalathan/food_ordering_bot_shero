"""Typed application settings.

Everything the service needs comes from the environment (see `.env.example`).
Nothing is hardcoded: credentials, base URLs and business rules are all here so
a deployment can be re-pointed (test -> live Stripe, zoho.com -> zoho.in)
without touching code.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
SourceMode = Literal["local", "remote"]
# How a customer browses: a link to the web storefront, or WhatsApp lists.
OrderMode = Literal["web", "chat"]
DistanceMode = Literal["haversine", "google_matrix"]

# Zoho runs region-isolated stacks; the OAuth and API hosts must match the data
# centre the client's org actually lives in, otherwise every call 401s.
ZOHO_HOSTS: dict[str, tuple[str, str]] = {
    "com": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "ca": ("https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
}


def is_unset(value: str | None) -> bool:
    """True when a setting is blank or is obviously a placeholder.

    python-dotenv keeps everything after `=` as the value, so a line like
    `KEY=    # [REQUIRED] paste it here` silently sets KEY to that comment.
    Without this check the readiness probe would report a completely
    unconfigured deployment as ready.
    """
    text = (value or "").strip()
    if not text:
        return True
    # A comment that leaked in as a value.
    if text.startswith("#"):
        return True
    # Common placeholder spellings left behind in a copied template.
    lowered = text.lower()
    return lowered in {
        "changeme", "change-me", "todo", "tbd", "xxx", "none", "null",
        "your-key-here", "<your-key>", "placeholder",
    }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Application ---------------------------------------------------------
    app_env: Environment = "development"
    log_level: str = "INFO"
    enable_scheduler: bool = True
    public_base_url: str = "http://localhost:8000"
    pay_redirect_base_url: str = "http://localhost:8000"

    # --- Database ------------------------------------------------------------
    # Any Postgres URL works (postgres://, postgresql://, ?sslmode=require as
    # Neon/Vercel give it); app/db/url.py turns it into the asyncpg form.
    database_url: str = "postgresql+asyncpg://shero:shero@localhost:5432/shero_bot"

    # --- Hosting on Vercel (docs/vercel-hosting.md) ---------------------------
    # Set to "1" by Vercel itself. Serverless: no in-process scheduler (Vercel
    # Cron calls /cron/*), no connection pool kept between requests, files
    # written only under /tmp.
    vercel: str = ""
    # Vercel sends it as "Authorization: Bearer <CRON_SECRET>" on every cron
    # call; /cron/* refuses any other caller, and refuses everyone when unset.
    cron_secret: str = ""
    # Set by Vercel when a Blob store is connected. With it, dish photos are
    # stored in Vercel Blob instead of on disk (services/media.py).
    blob_read_write_token: str = ""
    # Where dish photos are written when Blob is not used. Default data/media;
    # /tmp/shero-media on Vercel.
    media_dir: str = ""

    # --- Gallabox ------------------------------------------------------------
    gallabox_api_key: str = ""
    gallabox_api_secret: str = ""
    gallabox_channel_id: str = ""
    # Only needed to create/inspect message templates, not to send messages.
    gallabox_account_id: str = ""
    gallabox_base_url: str = "https://server.gallabox.com/devapi"
    gallabox_webhook_token: str = ""
    # The business's own WhatsApp number, digits with country code
    # (e.g. 14438011011). Powers the "Open WhatsApp" button after a web
    # order; left empty, the page simply does not show it.
    whatsapp_business_number: str = ""

    # --- Ops API (outlet staff marking orders) -------------------------------
    ops_api_key: str = ""

    # --- Admin dashboard -----------------------------------------------------
    admin_session_secret: str = ""
    admin_session_hours: int = 12
    # Fernet key encrypting secrets saved through the Settings page.
    settings_encryption_key: str = ""
    # Bootstrap login, used once to create the first admin user.
    admin_bootstrap_email: str = ""
    admin_bootstrap_password: str = ""

    # --- Meta ----------------------------------------------------------------
    meta_graph_version: str = "v21.0"
    meta_catalog_id: str = ""
    meta_system_user_token: str = ""
    meta_catalog_cache_ttl_seconds: int = 900

    # --- Zoho ----------------------------------------------------------------
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_refresh_token: str = ""
    zoho_data_center: str = "com"
    zoho_orders_module: str = "Orders"
    # The Orders module's display field, which holds the order number: "Name"
    # unless the module was created by hand with another (setup script warns).
    zoho_orders_name_field: str = "Name"
    # The bot's line-items module (one record per dish on a paid order).
    zoho_order_items_module: str = "Order_Items"
    # Which Zoho org the bot is connected to, recorded by Connect Zoho. Record
    # ids belong to one org; connecting another drops the saved links.
    zoho_org_id: str = ""

    # --- Uber Direct ---------------------------------------------------------
    uber_customer_id: str = ""
    uber_client_id: str = ""
    uber_client_secret: str = ""
    uber_scope: str = "eats.deliveries"
    uber_auth_url: str = "https://auth.uber.com/oauth/v2/token"
    uber_api_base_url: str = "https://api.uber.com"
    # Book the courier this long before the delivery slot starts, so they
    # can collect from the kitchen and arrive inside the slot.
    uber_dispatch_hours_before: float = Field(default=2.0, gt=0)

    # --- Stripe --------------------------------------------------------------
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_currency: str = "usd"
    payment_link_ttl_minutes: int = 30
    payment_reminder_minutes: int = 15

    # --- Optional customer messages ------------------------------------
    # Both are in the original spec but switched off at the client's
    # request. The templates stay approved on the WABA either way, so
    # turning one back on is this flag and nothing else - no resubmission.
    # Off means the *message* is skipped, not the step: an unpaid order
    # still expires, and an order still moves through the kitchen stages.
    send_payment_reminder: bool = False
    send_delivery_updates: bool = False

    # --- Who the bot answers ---------------------------------------------
    # Set ONLY on the admin Bot replies page (stored in the database), never
    # from .env: the alias below is a name no environment uses, so a stray
    # BOT_ALLOWED_NUMBERS line cannot restrict the bot. Nothing saved means
    # the bot replies to everyone. See services/allowlist.py.
    bot_reply_mode: str = Field(default="", validation_alias="ADMIN_PAGE_ONLY_BOT_REPLY_MODE")
    # Whitelisted WhatsApp numbers, "number|name,...".
    bot_allowed_numbers: str = Field(default="",
                                     validation_alias="ADMIN_PAGE_ONLY_BOT_ALLOWED_NUMBERS")
    # Reply to any message ("any", the default) or only start on a trigger
    # keyword ("keywords"), and the keywords as JSON. Admin page only, like
    # the two above. See services/reply_triggers.py.
    bot_reply_trigger: str = Field(default="", validation_alias="ADMIN_PAGE_ONLY_BOT_REPLY_TRIGGER")
    bot_trigger_keywords: str = Field(default="",
                                      validation_alias="ADMIN_PAGE_ONLY_BOT_TRIGGER_KEYWORDS")

    # --- Geo -----------------------------------------------------------------
    google_maps_api_key: str = ""
    # A second key for the map drawn in the customer's browser. Anything a
    # page loads is public, so this one should be restricted in Google Cloud
    # to HTTP referrers (your domain) and the Maps JavaScript API only. The
    # server key above stays private. Empty: the page draws an OpenStreetMap
    # map instead, while addresses still come from Google via the server.
    google_maps_browser_key: str = ""
    distance_mode: DistanceMode = "haversine"
    geocoder_country: str = "US"

    # --- Client backend adapters ---------------------------------------------
    order_mode: OrderMode = "web"
    outlet_source: SourceMode = "local"
    slot_source: SourceMode = "local"
    client_backend_base_url: str = ""
    client_backend_api_key: str = ""

    # --- Business rules ------------------------------------------------------
    # Stand-in delivery fee for local testing, used only when Uber is
    # unconfigured AND app_env is not "production". 0 disables it.
    delivery_fee_fallback: float = Field(default=0.0, ge=0)
    tax_percent: float = Field(default=0.0, ge=0, le=100)
    default_delivery_radius_km: float = Field(default=10.0, gt=0)
    slot_hold_minutes: int = 30
    # The earliest delivery slot offered starts this long after ordering:
    # home-style food is cooked to order, so the kitchen needs the day.
    slot_min_lead_hours: float = Field(default=24.0, ge=0)
    # The kitchen's new-order alert goes out at this hour (kitchen local
    # time) on the delivery day - never later than the Uber booking.
    kitchen_alert_hour: float = Field(default=7.0, ge=0, lt=24)
    feedback_delay_minutes: int = 30

    # --- Derived -------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def zoho_accounts_url(self) -> str:
        return ZOHO_HOSTS.get(self.zoho_data_center, ZOHO_HOSTS["com"])[0]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def zoho_api_url(self) -> str:
        return ZOHO_HOSTS.get(self.zoho_data_center, ZOHO_HOSTS["com"])[1]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def meta_graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.meta_graph_version}"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_serverless(self) -> bool:
        """Running as a Vercel Function rather than a long-lived server."""
        return self.vercel.strip() == "1"

    def missing_credentials(self) -> list[str]:
        """Names of credentials that are still blank or obviously unfilled.

        Used by the /health/readiness probe and by `scripts/check_config.py` so
        a half-configured deployment fails loudly instead of at 3am on a live
        customer's payment.
        """
        required = {
            "GALLABOX_API_KEY": self.gallabox_api_key,
            "GALLABOX_API_SECRET": self.gallabox_api_secret,
            "GALLABOX_CHANNEL_ID": self.gallabox_channel_id,
            "GALLABOX_WEBHOOK_TOKEN": self.gallabox_webhook_token,
            # META_CATALOG_ID / META_SYSTEM_USER_TOKEN are optional: the menu
            # is served from this database, not the Meta catalogue.
            "ZOHO_CLIENT_ID": self.zoho_client_id,
            "ZOHO_CLIENT_SECRET": self.zoho_client_secret,
            "ZOHO_REFRESH_TOKEN": self.zoho_refresh_token,
            "UBER_CUSTOMER_ID": self.uber_customer_id,
            "UBER_CLIENT_ID": self.uber_client_id,
            "UBER_CLIENT_SECRET": self.uber_client_secret,
            "STRIPE_SECRET_KEY": self.stripe_secret_key,
            "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret,
            "OPS_API_KEY": self.ops_api_key,
            "ADMIN_SESSION_SECRET": self.admin_session_secret,
            "SETTINGS_ENCRYPTION_KEY": self.settings_encryption_key,
        }
        return sorted(name for name, value in required.items() if is_unset(value))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
